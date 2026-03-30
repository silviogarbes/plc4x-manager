"""
pylogix Microservice — REST API for PLC discovery and diagnostics.

Runs on port 5000 inside the PLC4X server container.
Only accessible from the Docker internal network (backend).

Endpoints:
    GET  /health                                       — service health
    POST /discover?ip=X&path=1,0                       — full tag discovery
    POST /discover/tags?ip=X&path=1,0                  — controller tags only
    POST /discover/programs?ip=X&path=1,0              — list programs
    POST /discover/program-tags?ip=X&program=Y         — tags in a program
    POST /diagnostics/identity?ip=X&path=1,0           — PLC identity
    POST /diagnostics/health?ip=X&path=1,0             — full PLC health
    POST /diagnostics/read?ip=X&tag=Y&type=DINT        — read single tag
    GET  /stats                                        — connection statistics
"""

import ipaddress
import json
import logging
import re
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

from plctag_safety import PLCConnectionError, PLCBusyError, PLCRateLimitError, PLCReadOnlyError, is_plc_readonly

logging.basicConfig(
    level=logging.INFO,
    format="[PLCTag] %(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("plctag")

PORT = 5000


class PLCTagHandler(BaseHTTPRequestHandler):

    def _send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, message, status=500):
        self._send_json({"error": message}, status)

    def _get_params(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        return {k: v[0] for k, v in params.items()}

    # ------------------------------------------------------------------
    # GET handlers
    # ------------------------------------------------------------------

    def do_GET(self):
        path = urlparse(self.path).path

        if path == "/health":
            self._send_json({
                "status": "ok",
                "service": "plctag",
                "plcReadOnly": is_plc_readonly(),
            })

        elif path == "/stats":
            from plctag_safety import get_connection_stats
            self._send_json(get_connection_stats())

        else:
            self._send_error("Not found", 404)

    # ------------------------------------------------------------------
    # POST handlers
    # ------------------------------------------------------------------

    def do_POST(self):
        path = urlparse(self.path).path
        params = self._get_params()
        ip = params.get("ip")
        plc_path = params.get("path", "1,0")
        timeout = int(params.get("timeout", "5000"))

        if not ip:
            self._send_error("Missing 'ip' parameter", 400)
            return

        # I1: Validate IP address format
        try:
            ipaddress.ip_address(ip)
        except ValueError:
            self._send_error("Invalid IP address format", 400)
            return

        # I2: Validate path format — must be "digits,digits" (e.g., "1,0")
        if not re.match(r'^\d+,\d+$', plc_path):
            self._send_error("Invalid path format. Expected: slot,port (e.g., 1,0)", 400)
            return

        try:
            if path == "/discover":
                from plctag_discovery import discover_all
                result = discover_all(ip, plc_path, timeout)
                self._send_json(result)

            elif path == "/discover/tags":
                from plctag_discovery import list_tags
                tags = list_tags(ip, plc_path, timeout)
                self._send_json({"tags": tags, "count": len(tags)})

            elif path == "/discover/programs":
                from plctag_discovery import list_programs
                programs = list_programs(ip, plc_path, timeout)
                self._send_json({"programs": programs, "count": len(programs)})

            elif path == "/discover/program-tags":
                program = params.get("program")
                if not program:
                    self._send_error("Missing 'program' parameter", 400)
                    return
                from plctag_discovery import list_program_tags
                tags = list_program_tags(ip, program, plc_path, timeout)
                self._send_json({"program": program, "tags": tags, "count": len(tags)})

            elif path == "/diagnostics/identity":
                from plctag_diagnostics import get_plc_identity
                result = get_plc_identity(ip, plc_path, timeout)
                self._send_json(result)

            elif path == "/diagnostics/health":
                from plctag_diagnostics import get_plc_health
                result = get_plc_health(ip, plc_path, timeout)
                self._send_json(result)

            elif path == "/diagnostics/read":
                tag_name = params.get("tag")
                tag_type = params.get("type", "DINT")
                if not tag_name:
                    self._send_error("Missing 'tag' parameter", 400)
                    return
                from plctag_diagnostics import read_single_tag
                result = read_single_tag(ip, tag_name, tag_type, plc_path, timeout)
                self._send_json(result)

            elif path == "/diagnostics/write":
                if is_plc_readonly():
                    self._send_error("System is in read-only mode", 403)
                    return
                tag_name = params.get("tag")
                value = params.get("value")
                if not tag_name or value is None:
                    self._send_error("Missing 'tag' or 'value' parameter", 400)
                    return
                from plctag_diagnostics import write_single_tag
                result = write_single_tag(ip, tag_name, value, plc_path, timeout)
                self._send_json(result)

            elif path == "/diagnostics/batch-read":
                tags_param = params.get("tags", "")
                if not tags_param:
                    self._send_error("Missing 'tags' parameter (comma-separated)", 400)
                    return
                tag_list = [t.strip() for t in tags_param.split(",") if t.strip()]
                from plctag_diagnostics import batch_read_tags
                result = batch_read_tags(ip, tag_list, plc_path, timeout)
                self._send_json(result)

            elif path == "/diagnostics/batch-write":
                if is_plc_readonly():
                    self._send_error("System is in read-only mode", 403)
                    return
                # Expect JSON body with [{tag, value}, ...]
                content_length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(content_length)) if content_length > 0 else None
                if not body or not isinstance(body, list):
                    self._send_error("Expected JSON array of {tag, value} objects", 400)
                    return
                pairs = []
                for item in body:
                    tag_name = item.get("tag")
                    value = item.get("value")
                    if not tag_name or value is None:
                        self._send_error(f"Missing tag or value in item: {item}", 400)
                        return
                    # Auto-cast
                    if isinstance(value, str):
                        if value.lower() in ("true", "false"):
                            value = value.lower() == "true"
                        else:
                            try:
                                value = float(value) if "." in value else int(value)
                            except ValueError:
                                pass
                    pairs.append((tag_name, value))
                from plctag_diagnostics import batch_write_tags
                result = batch_write_tags(ip, pairs, plc_path, timeout)
                self._send_json(result)

            else:
                self._send_error("Not found", 404)

        except PLCReadOnlyError as e:
            self._send_error(str(e), 403)
        except PLCRateLimitError as e:
            self._send_error(str(e), 429)
        except PLCBusyError as e:
            self._send_error(str(e), 409)
        except PLCConnectionError as e:
            self._send_error(str(e), 503)
        except Exception as e:
            self._send_error(str(e), 500)

    def log_message(self, format, *args):
        log.info(f"{self.client_address[0]} - {args[0]}")


def main():
    log.info(f"Starting pylogix service on port {PORT}...")
    server = HTTPServer(("0.0.0.0", PORT), PLCTagHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down...")
        server.server_close()


if __name__ == "__main__":
    main()
