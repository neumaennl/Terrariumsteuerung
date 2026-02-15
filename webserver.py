"""
Minimal HTTP web server for terrarium controller.
Provides REST API and serves static HTML interface.
Uses async/await for non-blocking operation.
"""

import asyncio
import json
import time
import gc
import config
import terrariumsteuerung as ctrl
import storage
import os


# Module-level server handle for graceful stop
_server = None

# Constants
EPOCH_OFFSET = const(946684800)
HEADER_READ_CHUNK = const(512)
MAX_HEADER_BYTES = const(4096)


# MicroPython: os.path may be missing. Provide a small exists() helper using os.stat
def _exists(path):
    try:
        os.stat(path)
        return True
    except Exception:
        return False


# Simple HTTP response helpers
def http_response(status, body, content_type='text/plain'):
    """Create HTTP response."""
    status_text = {
        200: 'OK',
        201: 'Created',
        400: 'Bad Request',
        404: 'Not Found',
        500: 'Internal Server Error',
    }.get(status, 'Unknown')
    
    # Convert body to bytes
    if isinstance(body, (dict, list)):
        body = json.dumps(body).encode()
        content_type = 'application/json'
    elif isinstance(body, str):
        body = body.encode()
    elif not isinstance(body, bytes):
        body = str(body).encode()
    
    headers = (
        f'HTTP/1.1 {status} {status_text}\r\n'
        f'Content-Type: {content_type}\r\n'
        f'Content-Length: {len(body)}\r\n'
        f'Connection: close\r\n'
        '\r\n'
    ).encode()
    
    return headers + body


async def _write_chunk(writer, data_bytes):
    """Write a single HTTP chunk."""
    if not data_bytes:
        return
    writer.write(("%x\r\n" % len(data_bytes)).encode())
    writer.write(data_bytes)
    writer.write(b"\r\n")


async def stream_json_array(writer, items):
    """Stream a JSON array using chunked transfer encoding."""
    headers = (
        'HTTP/1.1 200 OK\r\n'
        'Content-Type: application/json\r\n'
        'Transfer-Encoding: chunked\r\n'
        'Connection: close\r\n'
        '\r\n'
    ).encode()
    writer.write(headers)
    await writer.drain()

    await _write_chunk(writer, b'[')
    first = True
    count = 0
    for item in items:
        item_bytes = json.dumps(item).encode()
        if first:
            chunk = item_bytes
            first = False
        else:
            chunk = b',' + item_bytes
        await _write_chunk(writer, chunk)
        count += 1
        if count % 50 == 0:
            gc.collect()
            await asyncio.sleep(0)

    await _write_chunk(writer, b']')
    writer.write(b"0\r\n\r\n")
    await writer.drain()


def get_api_data():
    """Get current sensor and status data."""
    unix_timestamp = int(time.time()) + EPOCH_OFFSET
    
    return {
        'temperature': round(ctrl.get_temperature(), 1),
        'humidity': round(ctrl.get_humidity(), 1),
        'rpm': ctrl.get_rpm(),
        'fan_pwm': ctrl.get_fan_pwm(),
        'pump_status': ctrl.get_pump_status(),
        'FAN_TARGET_HUMIDITY': ctrl.get_threshold_value('FAN_TARGET_HUMIDITY'),
        'PUMP_TRIGGER_HUMIDITY': ctrl.get_threshold_value('PUMP_TRIGGER_HUMIDITY'),
        'PUMP_EMERGENCY_OFF': ctrl.get_threshold_value('PUMP_EMERGENCY_OFF'),
        'PUMP_SPRAY_DURATION': ctrl.get_threshold_value('PUMP_SPRAY_DURATION'),
        'PUMP_COOLDOWN_MINUTES': ctrl.get_threshold_value('PUMP_COOLDOWN_MINUTES'),
        'NIGHT_START_HOUR': ctrl.get_threshold_value('NIGHT_START_HOUR'),
        'NIGHT_END_HOUR': ctrl.get_threshold_value('NIGHT_END_HOUR'),
        'DATA_REFRESH_INTERVAL': config.get('DATA_REFRESH_INTERVAL', 30),
        'HISTORY_REFRESH_INTERVAL': config.get('HISTORY_REFRESH_INTERVAL', 300),
        'timestamp': unix_timestamp,
    }


async def close_writer(writer):
    """Close writer connection safely."""
    try:
        writer.close()
        if hasattr(writer, 'wait_closed'):
            await writer.wait_closed()
    except Exception:
        pass


async def read_request_fully(reader, header_blob, body_blob):
    """Read full request body based on Content-Length header."""
    content_length = 0
    try:
        for line in header_blob.split(b"\r\n")[1:]:
            if b":" in line:
                key, value = line.split(b":", 1)
                if key.strip().lower() == b"content-length":
                    content_length = int(value.strip() or 0)
                    break
    except Exception:
        content_length = 0

    # Read remaining body if needed
    if content_length and len(body_blob) < content_length:
        remaining = content_length - len(body_blob)
        chunks = []
        while remaining > 0:
            more = await reader.read(min(HEADER_READ_CHUNK, remaining))
            if not more:
                break
            chunks.append(more)
            remaining -= len(more)
        if chunks:
            body_blob += b"".join(chunks)
    if content_length and len(body_blob) > content_length:
        body_blob = body_blob[:content_length]
    
    return body_blob


async def handle_api_data(writer, path):
    """Handle /api/data request."""
    data = get_api_data()
    print(f"[WEB] /api/data response: timestamp={data['timestamp']} (Unix epoch)")
    response = http_response(200, data)
    del data
    return response


async def handle_api_history(writer, path, reader):
    """Handle /api/history request."""
    params = {}
    if '?' in path:
        resource, query = path.split('?', 1)
    else:
        query = ''
    for part in query.split('&'):
        if '=' in part:
            k, v = part.split('=', 1)
            params[k] = v
    
    del resource, query
    gc.collect()

    start = None
    end = None
    limit = 500
    
    if 'start' in params:
        try:
            start = float(params['start'])
        except Exception:
            start = None
    if 'end' in params:
        try:
            end = float(params['end'])
        except Exception:
            end = None
    if 'limit' in params:
        try:
            limit = int(params['limit'])
        except Exception:
            limit = 500

    del params
    current_rtc_time = int(time.time())
    
    if start is not None and start > EPOCH_OFFSET:
        start = start - EPOCH_OFFSET
    if end is not None and end > EPOCH_OFFSET:
        end =end - EPOCH_OFFSET
    
    if start is not None and start < 0:
        start = 0
    if end is not None and end > current_rtc_time:
        end = current_rtc_time
    
    gc.collect()
    
    try:
        data = storage.get_readings(limit=limit, start_ts=start, end_ts=end)
        print(f"[WEB] History query: start={start}, end={end}, limit={limit}, result count={len(data)}")
        
        if len(data) == 0 and (start is not None or end is not None):
            print(f"[WEB] No data in range (possible clock skew), fetching all available readings")
            data = storage.get_readings(limit=limit, start_ts=None, end_ts=None)
            print(f"[WEB] Got {len(data)} total readings without time filters")
        
        for reading in data:
            reading['ts'] = reading['ts'] + EPOCH_OFFSET
        
        if data:
            print(f"[WEB] First reading after conversion: ts={data[0]['ts']} (Unix epoch, should be ~1.77B)")
                
    except Exception as e:
        print(f"[WEB] History fetch error: {e}")
        data = []

    try:
        await stream_json_array(writer, data)
    except Exception as e:
        print(f"[WEB] History stream error: {e}")
        response = http_response(500, {'error': 'history stream failed'})
        writer.write(response)
        await writer.drain()
    finally:
        del data
        gc.collect()
    return None


async def handle_api_settings(writer, body_blob):
    """Handle /api/settings POST request."""
    try:
        settings = json.loads(body_blob.decode('utf-8'))
        
        config_fields = ['DATA_REFRESH_INTERVAL', 'HISTORY_REFRESH_INTERVAL']
        
        for key, value in settings.items():
            if key in config_fields:
                config.set(key, value)
            else:
                ctrl.set_threshold_value(key, value)
        
        del settings
        response = http_response(200, {'ok': True})
    except Exception as e:
        print(f"[WEB] Settings error: {e}")
        response = http_response(400, {'error': str(e)})
    
    return response


async def stream_html_response(writer):
    """Stream HTML response with on-the-fly placeholder substitution (memory-efficient)."""
    try:
        data = get_api_data()
        # Build small dict of replacements for quick lookup
        replacements = {}
        for key, value in data.items():
            val_str = str(value)
            replacements['{{ data.' + key + ' }}'] = val_str
            replacements['{{data.' + key + '}}'] = val_str
            replacements['{{ data.' + key + '}}'] = val_str
            replacements['{{data.' + key + ' }}'] = val_str
        del data
        gc.collect()
        
        path = "index.html"
        if not _exists(path):
            # No template available, send error
            response = http_response(200, '<html><body><h1>No template found</h1></body></html>', 'text/html')
            writer.write(response)
            await writer.drain()
            return
        
        # Send headers first
        headers = (
            'HTTP/1.1 200 OK\r\n'
            'Content-Type: text/html; charset=utf-8\r\n'
            'Connection: close\r\n'
            '\r\n'
        ).encode()
        writer.write(headers)
        await writer.drain()
        del headers
        
        # Stream file in chunks with overlap buffer to handle placeholders spanning boundaries
        chunk_size = 2048  # Increased from 512 to reduce write/drain cycles
        overlap_size = 50  # Keep last 50 chars from previous chunk to catch split placeholders
        overlap_buffer = ''
        
        try:
            with open(path, 'r') as f:
                while True:
                    chunk = f.read(chunk_size)
                    if not chunk:
                        # Last chunk: flush overlap buffer with replacements
                        if overlap_buffer:
                            for placeholder, value in replacements.items():
                                overlap_buffer = overlap_buffer.replace(placeholder, value)
                            writer.write(overlap_buffer.encode())
                        # Final drain to ensure all data is sent before closing
                        await writer.drain()
                        break
                    
                    # Prepend overlap from previous chunk
                    full_text = overlap_buffer + chunk
                    
                    # Do ALL replacements on the full concatenated text FIRST
                    # This ensures placeholders spanning boundaries are caught
                    for placeholder, value in replacements.items():
                        full_text = full_text.replace(placeholder, value)
                    
                    # Now extract overlap for next iteration (after replacements)
                    # Keep last overlap_size characters (of the replaced text)
                    if len(full_text) > overlap_size:
                        to_send = full_text[:-overlap_size]
                        overlap_buffer = full_text[-overlap_size:]
                    else:
                        overlap_buffer = full_text
                        to_send = ''
                    
                    # Send the portion (already replaced above)
                    if to_send:
                        writer.write(to_send.encode())
                        # Drain less frequently to reduce connection overhead
                        if len(to_send) >= chunk_size - overlap_size:
                            await writer.drain()
                            await asyncio.sleep(0.005)  # Small delay to prevent overwhelming the network stack
        except Exception as e:
            print(f"[WEB] Stream error: {e}")
        
        del replacements
        gc.collect()
        
    except Exception as e:
        print(f"[WEB] Template stream error: {e}")


async def handle_client(reader, writer):
    """Handle an incoming HTTP connection with minimal memory footprint."""
    try:
        print("[WEB] Client connected")
        gc.collect()
        
        # Read request (buffer sized for typical requests)
        raw = await reader.read(1024)
        if not raw:
            await close_writer(writer)
            return

        # Ensure full headers are read (and avoid unbounded growth)
        while b"\r\n\r\n" not in raw and len(raw) < MAX_HEADER_BYTES:
            more = await reader.read(HEADER_READ_CHUNK)
            if not more:
                break
            raw += more

        print(f"[WEB] Raw request: {raw[:100]}... (length: {len(raw)} bytes)")

        # Split headers/body at blank line
        header_blob, body_blob = raw, b''
        if b"\r\n\r\n" in raw:
            header_blob, body_blob = raw.split(b"\r\n\r\n", 1)
        
        del raw
        gc.collect()

        # Parse request line
        try:
            first_line = header_blob.split(b"\r\n", 1)[0].decode('utf-8').strip()
        except Exception:
            first_line = ''

        if not first_line:
            await close_writer(writer)
            return

        parts = first_line.split(' ')
        if len(parts) < 2:
            await close_writer(writer)
            return

        method = parts[0]
        path = parts[1]

        # Read full body if needed
        body_blob = await read_request_fully(reader, header_blob, body_blob)
        del header_blob, parts, first_line
        gc.collect()
        
        # Route request
        response = None
        
        if path == '/' or path == '/index.html':
            await stream_html_response(writer)
            await asyncio.sleep(0.01)
            await close_writer(writer)
            gc.collect()
            return
        
        elif path == '/api/data':
            response = await handle_api_data(writer, path)
        
        elif path.startswith('/api/history'):
            await handle_api_history(writer, path, reader)
            await close_writer(writer)
            gc.collect()
            return
        
        elif path == '/api/settings' and method == 'POST':
            response = await handle_api_settings(writer, body_blob)
        
        elif (path.startswith('/api/reset_thresholds_defaults') or path.startswith('/api/reset-thresholds-defaults')) and method == 'POST':
            try:
                ctrl.reset_thresholds_to_defaults()
                response = http_response(200, {'ok': True})
            except Exception as e:
                print(f"[WEB] Reset thresholds error: {e}")
                response = http_response(500, {'ok': False, 'error': str(e)})

        else:
            response = http_response(404, 'Not Found')
        
        del path, method, body_blob
        gc.collect()
        
        # Send response
        if response:
            writer.write(response)
            await writer.drain()
            del response
            await asyncio.sleep(0.01)
        
        await close_writer(writer)
        gc.collect()
        
    except Exception as e:
        print(f"[WEB] Client handler error: {e}")
        await close_writer(writer)



async def run(host='0.0.0.0', port=80):
    """
    Start the HTTP web server with memory optimization.
    
    Args:
        host: Bind address (default: 0.0.0.0)
        port: Bind port (default: 80)
    """
    print(f"[WEB] Starting server on {host}:{port}")
    
    # Force garbage collection before server start
    gc.collect()
    print(f"[WEB] Free RAM before server: {gc.mem_free()} bytes")
    
    global _server
    try:
        server = await asyncio.start_server(handle_client, host, port)
        _server = server
        print(f"[WEB] Server listening on port {port}")

        # Some MicroPython uasyncio Server objects don't implement serve_forever().
        # Keep the task alive until cancelled and close server on stop().
        try:
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            pass
        finally:
            try:
                if hasattr(server, 'close'):
                    server.close()
                if hasattr(server, 'wait_closed'):
                    await server.wait_closed()
            except Exception:
                pass

    except OSError as e:
        print(f"[WEB] Error starting server: {e}")
        print(f"[WEB] Free RAM at error: {gc.mem_free()} bytes")
    except Exception as e:
        print(f"[WEB] Server error: {e}")


def stop():
    """Stop the web server (handled by asyncio context)."""
    global _server
    print("[WEB] Server stop requested")
    try:
        if _server is not None:
            try:
                _server.close()
            except Exception:
                pass
            _server = None
    except Exception:
        pass
