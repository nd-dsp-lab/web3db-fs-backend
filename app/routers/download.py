"""Content delivery: single-file download, folder-as-zip download, and
cached JPEG thumbnails. All token-authenticated via the security helpers."""
import io
import os
import logging
from typing import Optional

import httpx
from fastapi import APIRouter, Header
from fastapi.responses import StreamingResponse, JSONResponse
from web3 import Web3

from configure import IPFS_GATEWAY_URL, contract
from security import verify_auth_token, can_download, require_download_access

logger = logging.getLogger(__name__)

router = APIRouter()


# endpoint for downloading a file from ipfs -> updated
@router.get("/download/{cid}/{filename}")
async def download_file_with_name(cid: str, filename: str, x_auth_token: Optional[str] = Header(None)):
    denied = require_download_access(cid, x_auth_token)
    if denied:
        return denied
    try:
        # follow_redirects: kubo's gateway 301-redirects /ipfs/{cid} to the
        # subdomain gateway ({cid}.ipfs.localhost)
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            # Get file from IPFS using GET (updated from POST)
            # response = await client.get(f"{IPFS_API_URL}/cat", params={"arg": cid})
            response = await client.get(f"{IPFS_GATEWAY_URL}/{cid}")
        if response.status_code != 200:
            raise Exception(f"Failed to fetch file from IPFS: {response.status_code}")

        return StreamingResponse(
            io.BytesIO(response.content),
            media_type="application/octet-stream",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )

    except Exception as e:
        logger.error("Download failed: %s", e)
        return JSONResponse(status_code=502, content={"error": f"Failed to download file: {str(e)}"})


# Download a whole folder as a zip. Token-authenticated; includes files under
# that path the token's address owns or has the DOWNLOAD permission on (so
# folders shared to the user are downloadable from the Shared view too).
@router.get("/download-folder")
async def download_folder_zip(path: str, x_auth_token: Optional[str] = Header(None)):
    import zipfile

    address = verify_auth_token(x_auth_token or "")
    if not address:
        return JSONResponse(status_code=401, content={"error": "Missing or invalid auth token"})

    prefix = "/" + "/".join(p for p in path.split("/") if p)
    if prefix == "/":
        return JSONResponse(status_code=400, content={"error": "Invalid folder path"})

    checksum = Web3.to_checksum_address(address)
    entries = []
    for f in contract.functions.getUserFiles(checksum).call():
        cid, full_path = f[0], f[1]
        norm = "/" + "/".join(p for p in full_path.split("/") if p)
        if not norm.startswith(prefix + "/"):
            continue
        if not can_download(cid, address):
            continue
        entries.append((cid, norm[len(prefix) + 1:]))

    if not entries:
        return JSONResponse(status_code=404, content={"error": "Folder is empty"})

    folder_name = prefix.rsplit("/", 1)[-1]
    buf = io.BytesIO()
    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for cid, rel in entries:
                response = await client.get(f"{IPFS_GATEWAY_URL}/{cid}")
                if response.status_code == 200:
                    zf.writestr(f"{folder_name}/{rel}", response.content)
                else:
                    logger.warning("download-folder: skipping %s (%s), gateway %s", cid, rel, response.status_code)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename={folder_name}.zip"},
    )


# --- Thumbnails ---
# Small JPEG previews for image files, generated once per CID with Pillow
# and cached on disk (content is immutable per CID, so the cache never
# stales). Non-image or undecodable content returns 404 and the frontend
# keeps its file-type icon.
THUMBS_DIR = os.path.join(os.path.dirname(__file__), "..", "thumbs")
THUMB_SIZE = (320, 320)


def render_text_thumbnail(content: bytes):
    """Drive-style page snippet for text files. Raises if content isn't UTF-8 text."""
    from PIL import Image, ImageDraw, ImageFont

    text = content[:8192].decode("utf-8")  # raises UnicodeDecodeError on binary
    img = Image.new("RGB", THUMB_SIZE, "#ffffff")
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("Menlo.ttc", 13)
    except OSError:
        font = ImageFont.load_default(size=13)

    x, y, line_height = 16, 14, 17
    for line in text.splitlines():
        if y > THUMB_SIZE[1] - line_height:
            break
        draw.text((x, y), line.replace("\t", "    ")[:60], fill="#3c4043", font=font)
        y += line_height
    return img


@router.get("/thumbnail/{cid}")
async def get_thumbnail(cid: str, x_auth_token: Optional[str] = Header(None)):
    from PIL import Image

    # CIDs are base32/base58 alphanumeric — reject anything path-like
    if not cid.isalnum():
        return JSONResponse(status_code=400, content={"error": "Invalid CID"})

    denied = require_download_access(cid, x_auth_token)
    if denied:
        return denied

    os.makedirs(THUMBS_DIR, exist_ok=True)
    thumb_path = os.path.join(THUMBS_DIR, f"{cid}.jpg")

    if not os.path.exists(thumb_path):
        try:
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                response = await client.get(f"{IPFS_GATEWAY_URL}/{cid}")
            if response.status_code != 200:
                return JSONResponse(status_code=404, content={"error": "File not found on IPFS"})

            content = response.content
            if content[:5] == b"%PDF-":
                # First page of a PDF, rendered via PyMuPDF
                import fitz
                doc = fitz.open(stream=content, filetype="pdf")
                pix = doc[0].get_pixmap(matrix=fitz.Matrix(0.5, 0.5))
                img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                doc.close()
            else:
                try:
                    img = Image.open(io.BytesIO(content))
                    img = img.convert("RGB")  # flatten alpha/palette for JPEG
                except Exception:
                    img = render_text_thumbnail(content)  # raises if binary
            img.thumbnail(THUMB_SIZE)
            img.save(thumb_path, "JPEG", quality=70)
        except Exception as e:
            logger.warning("Thumbnail generation failed for %s: %s", cid, e)
            return JSONResponse(status_code=404, content={"error": "Not a previewable image"})

    return StreamingResponse(
        open(thumb_path, "rb"),
        media_type="image/jpeg",
        headers={"Cache-Control": "private, max-age=86400"}
    )
