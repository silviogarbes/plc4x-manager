"""
PLC4X jar version management and connection template routes for PLC4X Manager FastAPI.

Endpoints:
  GET  /api/plc4x/version          — current jar version
  GET  /api/plc4x/latest-version   — check Maven Central
  POST /api/plc4x/update           — download new jar + restart (@require_admin)
  POST /api/plc4x/rollback         — rollback to backup jar (@require_admin)
  GET  /api/templates              — connection string templates
"""

from __future__ import annotations

import asyncio
import io
import os
import re
import tarfile
import tempfile
import urllib.request
import xml.etree.ElementTree as ET

import docker
from fastapi import APIRouter, Depends, HTTPException

from auth import CurrentUser, get_current_user, require_admin
from docker_utils import get_container_by_service, get_docker_client

router = APIRouter(tags=["plc4x"])

# (Docker helpers imported from docker_utils)


# =============================================
# Routes
# =============================================

def _get_plc4x_version_sync() -> dict:
    """Synchronous helper to read version from the PLC4X container."""
    try:
        client = get_docker_client()
        container = get_container_by_service(client, "plc4x-server")
        if container is None:
            return {"currentVersion": "unknown", "error": "Cannot read server container"}
        current_version = "unknown"
        backup_version = None

        for fname, target in [("version.txt", "current"), ("version.txt.bak", "backup")]:
            try:
                tar_stream, _ = container.get_archive(f"/app/{fname}")
                raw = b"".join(tar_stream)
                tar = tarfile.open(fileobj=io.BytesIO(raw))
                member = tar.getmembers()[0]
                v = tar.extractfile(member).read().decode("utf-8").strip()
                if v:
                    if target == "current":
                        current_version = v
                    else:
                        backup_version = v
            except Exception:
                pass

        # Fallback: read from container env var
        if current_version == "unknown":
            env_vars = container.attrs.get("Config", {}).get("Env", [])
            for env in env_vars:
                if env.startswith("PLC4X_VERSION="):
                    current_version = env.split("=", 1)[1]
                    break

        return {
            "currentVersion": current_version,
            "backupVersion": backup_version,
            "container": container.short_id
        }
    except Exception:
        return {"currentVersion": "unknown", "error": "Cannot read server container"}


@router.get("/api/plc4x/version")
async def api_plc4x_version(user: CurrentUser = Depends(get_current_user)):
    """Returns the current PLC4X uber-jar version and available info."""
    return await asyncio.to_thread(_get_plc4x_version_sync)


def _fetch_latest_version_sync() -> dict:
    """Synchronous helper to fetch latest version from Maven Central."""
    metadata_url = "https://repo1.maven.org/maven2/org/apache/plc4x/plc4x-opcua-server/maven-metadata.xml"
    req = urllib.request.Request(metadata_url, headers={"User-Agent": "plc4x-manager/1.0"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        xml_data = resp.read()
    root = ET.fromstring(xml_data)
    latest = root.findtext(".//latest") or root.findtext(".//release")
    versions = [v.text for v in root.findall(".//versions/version") if v.text]
    return {"latestVersion": latest, "versions": versions[-10:]}


@router.get("/api/plc4x/latest-version")
async def api_plc4x_latest_version(user: CurrentUser = Depends(get_current_user)):
    """Checks Maven Central for the latest PLC4X OPC-UA Server version."""
    try:
        return await asyncio.to_thread(_fetch_latest_version_sync)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


def _do_plc4x_update_sync(version: str, jar_url: str) -> dict:
    """Synchronous implementation of the PLC4X jar update (called via asyncio.to_thread)."""
    # Step 1: Check if version exists on Maven Central
    req = urllib.request.Request(jar_url, method="HEAD", headers={"User-Agent": "plc4x-manager/1.0"})
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        raise ValueError(
            f"Version {version} not found on Maven Central. "
            "Check https://repo1.maven.org/maven2/org/apache/plc4x/plc4x-opcua-server/"
        )

    client = get_docker_client()
    container = get_container_by_service(client, "plc4x-server")
    if container is None:
        raise docker.errors.NotFound("plc4x-server container not found")

    # Step 2: Stop the container
    try:
        container.stop(timeout=5)
    except Exception:
        pass

    # Step 3: Download jar locally
    tmp_jar = os.path.join(tempfile.gettempdir(), "plc4x-update.jar")
    req = urllib.request.Request(jar_url, headers={"User-Agent": "plc4x-manager/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            with open(tmp_jar, "wb") as f:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
    except Exception:
        container.start()
        raise RuntimeError(f"Failed to download version {version} from Maven Central.")

    # Backup current jar and version files (get_archive works on stopped containers)
    for fname, bak_name in [("plc4x-opcua-server.jar", "plc4x-opcua-server.jar.bak"),
                              ("version.txt", "version.txt.bak")]:
        try:
            tar_stream, _ = container.get_archive(f"/app/{fname}")
            raw = b"".join(tar_stream)
            src_tar = tarfile.open(fileobj=io.BytesIO(raw))
            member = src_tar.getmembers()[0]
            file_data = src_tar.extractfile(member).read()
            # Re-package with the backup name
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w") as t:
                info = tarfile.TarInfo(name=bak_name)
                info.size = len(file_data)
                t.addfile(info, io.BytesIO(file_data))
            buf.seek(0)
            container.put_archive("/app", buf.read())
        except Exception:
            pass  # No existing file to backup (first run)

    # Copy new jar and version file into the container
    tar_buf = io.BytesIO()
    with tarfile.open(fileobj=tar_buf, mode="w") as tar:
        tar.add(tmp_jar, arcname="plc4x-opcua-server.jar")
        ver_data = version.encode("utf-8")
        ver_info = tarfile.TarInfo(name="version.txt")
        ver_info.size = len(ver_data)
        tar.addfile(ver_info, io.BytesIO(ver_data))
    tar_buf.seek(0)
    container.put_archive("/app", tar_buf.read())

    # Cleanup temp file
    try:
        os.unlink(tmp_jar)
    except Exception:
        pass

    # Step 4: Start the server
    container.start()

    return {
        "message": f"PLC4X updated to version {version}. Server restarting...",
        "version": version,
        "jarUrl": jar_url
    }


@router.post("/api/plc4x/update")
async def api_plc4x_update(body: dict, user: CurrentUser = Depends(require_admin)):
    """Updates the PLC4X uber-jar to a specified version.

    Downloads the new jar from Maven Central into the server container, then restarts.
    Request body: {"version": "0.13.1"}
    """
    if not body or "version" not in body:
        raise HTTPException(status_code=400, detail="Field 'version' is required")

    version = body["version"].strip()
    if not re.match(r'^[0-9]+\.[0-9]+\.[0-9]+(-[a-zA-Z0-9]+)?$', version):
        raise HTTPException(status_code=400, detail="Invalid version format. Use X.Y.Z (e.g., 0.13.1)")

    jar_url = (
        f"https://repo1.maven.org/maven2/org/apache/plc4x/plc4x-opcua-server/"
        f"{version}/plc4x-opcua-server-{version}.jar"
    )

    try:
        return await asyncio.to_thread(_do_plc4x_update_sync, version, jar_url)
    except docker.errors.NotFound:
        raise HTTPException(status_code=404, detail="Server container not found")
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error during update: {str(e)}")


def _do_plc4x_rollback_sync() -> dict:
    """Synchronous implementation of the PLC4X jar rollback (called via asyncio.to_thread)."""
    client = get_docker_client()
    container = get_container_by_service(client, "plc4x-server")
    if container is None:
        raise docker.errors.NotFound("plc4x-server container not found")

    # Check if backup jar exists
    try:
        container.get_archive("/app/plc4x-opcua-server.jar.bak")
    except docker.errors.NotFound:
        raise FileNotFoundError("No backup jar found. Cannot rollback.")

    # Stop the container first
    try:
        container.stop(timeout=5)
    except Exception:
        pass

    # Read all 4 files from the container
    files = {}
    for fname in ["plc4x-opcua-server.jar", "plc4x-opcua-server.jar.bak",
                  "version.txt", "version.txt.bak"]:
        try:
            tar_stream, _ = container.get_archive(f"/app/{fname}")
            raw = b"".join(tar_stream)
            src_tar = tarfile.open(fileobj=io.BytesIO(raw))
            member = src_tar.getmembers()[0]
            files[fname] = src_tar.extractfile(member).read()
        except Exception:
            pass

    # Build tar with swapped files: current <-> backup
    tar_buf = io.BytesIO()
    with tarfile.open(fileobj=tar_buf, mode="w") as tar:
        for src_name, dst_name in [
            ("plc4x-opcua-server.jar.bak", "plc4x-opcua-server.jar"),
            ("plc4x-opcua-server.jar", "plc4x-opcua-server.jar.bak"),
            ("version.txt.bak", "version.txt"),
            ("version.txt", "version.txt.bak"),
        ]:
            if src_name in files:
                info = tarfile.TarInfo(name=dst_name)
                info.size = len(files[src_name])
                tar.addfile(info, io.BytesIO(files[src_name]))
    tar_buf.seek(0)
    container.put_archive("/app", tar_buf.read())

    # Read the restored version
    rollback_version = "previous"
    if "version.txt.bak" in files:
        v = files["version.txt.bak"].decode("utf-8").strip()
        if v:
            rollback_version = v

    # Start the server
    container.start()
    return {"message": f"Rolled back to version {rollback_version}. Server restarting..."}


@router.post("/api/plc4x/rollback")
async def api_plc4x_rollback(user: CurrentUser = Depends(require_admin)):
    """Rolls back to the previous PLC4X jar version (from .bak file)."""
    try:
        return await asyncio.to_thread(_do_plc4x_rollback_sync)
    except docker.errors.NotFound:
        raise HTTPException(status_code=404, detail="Server container not found")
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error during rollback: {str(e)}")


@router.get("/api/templates")
async def api_connection_templates(user: CurrentUser = Depends(get_current_user)):
    """Returns connection string templates for all supported protocols."""
    return [
        {"protocol": "Siemens S7", "template": "s7://{{IP}}", "example": "s7://192.168.1.10",
         "tagExamples": [{"alias": "Temperature", "address": "%DB1:0:REAL"}, {"alias": "Counter", "address": "%DB1:4:DINT"},
                         {"alias": "MotorOn", "address": "%DB1:8.0:BOOL"}, {"alias": "Speed", "address": "%DB1:10:INT"}]},
        {"protocol": "Modbus TCP", "template": "modbus-tcp://{{IP}}:502", "example": "modbus-tcp://192.168.1.20:502",
         "tagExamples": [{"alias": "HoldingReg1", "address": "holding-register:1"}, {"alias": "Coil1", "address": "coil:1"},
                         {"alias": "InputReg1", "address": "input-register:1"}, {"alias": "DiscreteInput1", "address": "discrete-input:1"}]},
        {"protocol": "Modbus RTU", "template": "modbus-rtu://{{SERIAL_PORT}}", "example": "modbus-rtu:///dev/ttyUSB0",
         "tagExamples": [{"alias": "HoldingReg1", "address": "holding-register:1"}, {"alias": "Coil1", "address": "coil:1"}]},
        {"protocol": "Modbus ASCII", "template": "modbus-ascii://{{SERIAL_PORT}}", "example": "modbus-ascii:///dev/ttyUSB0",
         "tagExamples": [{"alias": "HoldingReg1", "address": "holding-register:1"}]},
        {"protocol": "OPC-UA (Client)", "template": "opcua:tcp://{{IP}}:4840", "example": "opcua:tcp://192.168.1.30:4840",
         "tagExamples": [{"alias": "Node1", "address": "ns=2;i=10"}, {"alias": "Node2", "address": "ns=2;s=MyVariable"}]},
        {"protocol": "EtherNet/IP", "template": "eip://{{IP}}", "example": "eip://192.168.1.40",
         "tagExamples": [{"alias": "Tag1", "address": "%MyTag:DINT"}]},
        {"protocol": "Allen-Bradley Logix", "template": "logix://{{IP}}", "example": "logix://192.168.1.50",
         "tagExamples": [{"alias": "Tag1", "address": "MyTag"}]},
        {"protocol": "KNXnet/IP", "template": "knxnet-ip://{{IP}}", "example": "knxnet-ip://192.168.1.60",
         "tagExamples": [{"alias": "Light1", "address": "1/1/1:DPT_Switch"}]},
        {"protocol": "IEC 60870-5-104", "template": "iec-60870-5-104://{{IP}}", "example": "iec-60870-5-104://192.168.1.70",
         "tagExamples": [{"alias": "MeasuredFloat", "address": "M_ME_NC_1:5:20"}]},
        {"protocol": "Firmata (Arduino)", "template": "firmata://{{SERIAL_PORT}}", "example": "firmata:///dev/ttyACM0",
         "tagExamples": [{"alias": "DigitalPin13", "address": "digital:13"}, {"alias": "AnalogPin0", "address": "analog:0"}]},
        {"protocol": "Simulated (Test)", "template": "simulated://127.0.0.1", "example": "simulated://127.0.0.1",
         "tagExamples": [{"alias": "RandomInt", "address": "RANDOM/Temporary:DINT"}, {"alias": "RandomFloat", "address": "RANDOM/Temporary:REAL"},
                         {"alias": "StateVar", "address": "STATE/Temporary:DINT"}, {"alias": "ProductCode", "address": "VIRTUAL"}]}
    ]
