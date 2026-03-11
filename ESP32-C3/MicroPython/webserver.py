"""
Minimal HTTP web server for terrarium controller.
Provides REST API endpoints to be used by a UI.
Uses async/await for non-blocking operation.
"""

import asyncio
import json
import time
import gc
import config
import terrariumsteuerung as ctrl
from ntp_sync import log_print as print, to_unix_timestamp, format_local_datetime


# Module-level server handle for graceful stop
_server = None

# Memory health telemetry
_mem_free = 0
_mem_alloc = 0
_mem_free_min = 0
_mem_last_sample = 0

# Constants
EPOCH_OFFSET = const(946684800)
HEADER_READ_CHUNK = const(512)
MAX_HEADER_BYTES = const(4096)


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


def get_api_data():
    """Get current sensor and status data."""
    global _mem_free, _mem_alloc, _mem_free_min, _mem_last_sample

    if _mem_last_sample == 0:
        # Fallback for first request before periodic sampler runs
        gc.collect()
        _mem_free = gc.mem_free()
        _mem_alloc = gc.mem_alloc()
        _mem_free_min = _mem_free
        _mem_last_sample = int(time.time())

    unix_timestamp = to_unix_timestamp(int(time.time()))
    mem_last_sample_unix = to_unix_timestamp(_mem_last_sample)
    
    return {
        'temperature': round(ctrl.get_temperature(), 1),
        'humidity': round(ctrl.get_humidity(), 1),
        'rpm': ctrl.get_rpm(),
        'fan_pwm': ctrl.get_fan_pwm(),
        'pump_status': ctrl.get_pump_status(),
        'FAN_TARGET_HUMIDITY': ctrl.get_threshold_value('FAN_TARGET_HUMIDITY'),
        'FAN_NIGHT_START_HOUR': ctrl.get_threshold_value('FAN_NIGHT_START_HOUR'),
        'FAN_NIGHT_END_HOUR': ctrl.get_threshold_value('FAN_NIGHT_END_HOUR'),
        'PUMP_TRIGGER_HUMIDITY': ctrl.get_threshold_value('PUMP_TRIGGER_HUMIDITY'),
        'PUMP_SPRAY_DURATION': ctrl.get_threshold_value('PUMP_SPRAY_DURATION'),
        'PUMP_COOLDOWN_MINUTES': ctrl.get_threshold_value('PUMP_COOLDOWN_MINUTES'),
        'PUMP_NIGHT_START_HOUR': ctrl.get_threshold_value('PUMP_NIGHT_START_HOUR'),
        'PUMP_NIGHT_END_HOUR': ctrl.get_threshold_value('PUMP_NIGHT_END_HOUR'),
        'DATA_REFRESH_INTERVAL': config.get('DATA_REFRESH_INTERVAL', 30),
        'HISTORY_REFRESH_INTERVAL': config.get('HISTORY_REFRESH_INTERVAL', 300),
        'mem_free': _mem_free,
        'mem_alloc': _mem_alloc,
        'mem_free_min': _mem_free_min,
        'mem_last_sample': mem_last_sample_unix,
        'mem_last_sample_local': format_local_datetime(_mem_last_sample),
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
    response = http_response(200, data)
    del data
    return response


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
        
        if path == '/api/data':
            response = await handle_api_data(writer, path)

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
    
    global _server, _mem_free, _mem_alloc, _mem_free_min, _mem_last_sample
    try:
        server = await asyncio.start_server(handle_client, host, port)
        _server = server
        print(f"[WEB] Server listening on port {port}")

        # Initialize memory telemetry immediately
        gc.collect()
        _mem_free = gc.mem_free()
        _mem_alloc = gc.mem_alloc()
        _mem_free_min = _mem_free
        _mem_last_sample = int(time.time())

        sample_interval = config.get('MEMORY_SAMPLE_INTERVAL_SECONDS', 60)
        log_interval = config.get('MEMORY_LOG_INTERVAL_SECONDS', 600)
        try:
            sample_interval = int(sample_interval)
        except Exception:
            sample_interval = 60
        try:
            log_interval = int(log_interval)
        except Exception:
            log_interval = 600
        if sample_interval < 10:
            sample_interval = 10
        if log_interval < sample_interval:
            log_interval = sample_interval
        next_log_ts = int(time.time()) + log_interval

        # Some MicroPython uasyncio Server objects don't implement serve_forever().
        # Keep the task alive until cancelled and close server on stop().
        try:
            while True:
                await asyncio.sleep(sample_interval)
                gc.collect()
                _mem_free = gc.mem_free()
                _mem_alloc = gc.mem_alloc()
                if _mem_free_min == 0 or _mem_free < _mem_free_min:
                    _mem_free_min = _mem_free
                _mem_last_sample = int(time.time())

                if _mem_last_sample >= next_log_ts:
                    print(f"[WEB][MEM] free={_mem_free} alloc={_mem_alloc} min_free={_mem_free_min}")
                    next_log_ts = _mem_last_sample + log_interval
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
