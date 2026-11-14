import asyncio
import datetime
import json
import logging
import os
import signal
import shutil
import sys
import tempfile
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urljoin, urlsplit, unquote
from xml.sax.saxutils import escape

import httpx
from PIL import Image
from pypdf import PdfReader, PdfWriter


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("escl-walkup")

NS_ESCL = "http://schemas.hp.com/imaging/escl/2011/05/03"
NS_PWG = "http://www.pwg.org/schemas/2010/12/sm"

PAPER_SIZES = {
    "A3": (3508, 4961),
    "A4": (2480, 3508),
    "A5": (1748, 2480),
    "A6": (1240, 1748),
    "A7": (874, 1240),
    "B4": (2953, 4169),
    "B5": (2079, 2953),
    "B6": (1476, 2079),
    "LETTER": (2550, 3300),
    "LEGAL": (2550, 4200),
    "EXECUTIVE": (2175, 3150),
    "STATEMENT": (1650, 2550),
}

MIME_MAP = {
    "application/pdf": ("PDF", ".pdf"),
    "image/jpeg": ("JPEG", ".jpg"),
    "image/png": ("PNG", ".png"),
    "image/tiff": ("TIFF", ".tif"),
}

MIME_TO_FORMAT = {
    "application/pdf": "pdf",
    "image/jpeg": "jpeg",
    "image/png": "png",
    "image/tiff": "tiff",
}

SHORTCUT_FORMAT_TO_MIME = {
    "pdf": "application/pdf",
    "jpeg": "image/jpeg",
    "jpg": "image/jpeg",
    "png": "image/png",
    "tiff": "image/tiff",
    "tif": "image/tiff",
}


def local_name(tag: str) -> str:
    """Return an XML tag name without its namespace."""
    return tag.rsplit("}", 1)[-1]


def find_text(root: ET.Element, wanted_name: str):
    """Find the first element by local name, regardless of XML namespace."""
    for element in root.iter():
        if local_name(element.tag) == wanted_name:
            return element.text.strip() if element.text else None
    return None


def find_int(root: ET.Element, wanted_name: str):
    value = find_text(root, wanted_name)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def descendants_text(root: ET.Element, wanted_name: str):
    return [
        element.text.strip()
        for element in root.iter()
        if local_name(element.tag) == wanted_name and element.text
    ]


def parse_source_capabilities(
    root: ET.Element,
    caps_name: str,
    *,
    supports_duplex: bool = False,
):
    """Parse capabilities for one physical input source, if advertised."""
    caps_node = next(
        (node for node in root.iter() if local_name(node.tag) == caps_name),
        None,
    )

    if caps_node is None:
        return None

    limits = {}
    limit_names = (
        "MinWidth", "MaxWidth", "MinHeight", "MaxHeight",
        "MinPageWidth", "MaxPageWidth",
        "MinPageHeight", "MaxPageHeight",
        "MaxScanRegions",
    )
    for name in limit_names:
        value = find_int(caps_node, name)
        if value is not None:
            limits[name] = value

    resolutions = set()
    resolution_ranges = []

    for node in caps_node.iter():
        node_type = local_name(node.tag)
        if node_type in ("DiscreteResolution", "Resolution"):
            x = find_int(node, "XResolution")
            y = find_int(node, "YResolution")
            if x and y and x == y:
                resolutions.add(x)

        elif node_type == "ResolutionRange":
            min_x = find_int(node, "MinXResolution")
            max_x = find_int(node, "MaxXResolution")
            min_y = find_int(node, "MinYResolution")
            max_y = find_int(node, "MaxYResolution")

            if None not in (min_x, max_x, min_y, max_y):
                lower = max(min_x, min_y)
                upper = min(max_x, max_y)
                if lower <= upper:
                    resolution_ranges.append({"min": lower, "max": upper})

    if not resolutions and not resolution_ranges:
        direct_x = [
            int(value) for value in descendants_text(caps_node, "XResolution")
            if value.isdigit()
        ]
        direct_y = [
            int(value) for value in descendants_text(caps_node, "YResolution")
            if value.isdigit()
        ]
        resolutions.update(x for x in direct_x if x in direct_y)

    color_modes = descendants_text(caps_node, "ColorMode")
    doc_formats = (
        descendants_text(caps_node, "DocumentFormatExt")
        or descendants_text(caps_node, "DocumentFormat")
    )

    return {
        **limits,
        "resolutions": sorted(resolutions),
        "resolution_ranges": resolution_ranges,
        "color_modes": list(dict.fromkeys(color_modes)),
        "doc_formats": list(dict.fromkeys(doc_formats)),
        "supports_duplex": supports_duplex,
    }


class ESCLWalkupScanner:
    def __init__(self, config_path: str = "config.json"):
        with open(config_path, "r", encoding="utf-8") as config_file:
            self.config = json.load(config_file)

        self.printer_url = self.config["printer_url"].rstrip("/")
        self.host_id = self.config.get("host_identifier", "Homelab-Scanner")
        self.verify_ssl = bool(self.config.get("verify_ssl", False))
        self.puid = int(self.config.get("puid", 1000))
        self.pgid = int(self.config.get("pgid", 1000))
        self.shortcuts = self.config.get("shortcuts", {})

        if not isinstance(self.shortcuts, dict):
            raise ValueError("'shortcuts' must be a JSON object.")

        self.default_paper_size = str(
            self.config.get("paper_size", "A4")
        ).upper()

        if self.default_paper_size not in PAPER_SIZES:
            raise ValueError(
                f"Unsupported paper_size '{self.default_paper_size}'. "
                f"Choose one of: {', '.join(PAPER_SIZES)}."
            )

        self.capabilities_cache = None

        self.client = httpx.AsyncClient(
            verify=self.verify_ssl,
            timeout=httpx.Timeout(120.0, connect=10.0),
            follow_redirects=True,
        )

        self.subscription_url = None
        self.subscription_id = None
        self.running = True

        self.current_session_documents = []
        self.current_shortcut_cfg = None
        self.session_temp_dir = None

    async def get_scanner_capabilities(self) -> dict:
        if self.capabilities_cache is not None:
            return self.capabilities_cache

        url = f"{self.printer_url}/eSCL/ScannerCapabilities"
        response = await self.client.get(url)
        response.raise_for_status()
        root = ET.fromstring(response.content)

        capability_blocks = {
            "Platen": ("PlatenInputCaps", False),
            "FeederSimplex": ("AdfSimplexInputCaps", False),
            "FeederDuplex": ("AdfDuplexInputCaps", True),
        }

        capabilities = {}
        for source, (xml_name, is_duplex) in capability_blocks.items():
            parsed = parse_source_capabilities(
                root, xml_name, supports_duplex=is_duplex
            )
            if parsed is not None:
                capabilities[source] = parsed

        if not capabilities:
            raise RuntimeError(
                "ScannerCapabilities did not advertise any supported input source."
            )

        self.capabilities_cache = capabilities

        for source, caps in capabilities.items():
            logger.info(
                "%s capabilities: resolutions=%s, color_modes=%s, doc_formats=%s, "
                "width=%s..%s, height=%s..%s, duplex=%s",
                source,
                caps["resolutions"],
                caps["color_modes"],
                caps["doc_formats"],
                caps.get("MinWidth"),
                caps.get("MaxWidth"),
                caps.get("MinHeight"),
                caps.get("MaxHeight"),
                caps["supports_duplex"],
            )

        return capabilities

    def _select_paper_size(
        self,
        shortcut_cfg: dict,
        input_source: str,
        caps: dict,
    ):
        paper_size = str(
            shortcut_cfg.get("paper_size", self.default_paper_size)
        ).upper()

        if paper_size not in PAPER_SIZES:
            raise ValueError(
                f"Unknown paper_size '{paper_size}'. "
                f"Supported: {', '.join(PAPER_SIZES)}"
            )

        width, height = PAPER_SIZES[paper_size]

        min_width = caps.get("MinWidth", 0)
        min_height = caps.get("MinHeight", 0)
        max_width = caps.get("MaxWidth")
        max_height = caps.get("MaxHeight")

        min_page_width = caps.get("MinPageWidth", 0)
        min_page_height = caps.get("MinPageHeight", 0)
        max_page_width = caps.get("MaxPageWidth") or None
        max_page_height = caps.get("MaxPageHeight") or None

        valid = (
            width >= min_width
            and height >= min_height
            and (max_width is None or width <= max_width)
            and (max_height is None or height <= max_height)
            and width >= min_page_width
            and height >= min_page_height
            and (max_page_width is None or width <= max_page_width)
            and (max_page_height is None or height <= max_page_height)
        )

        if not valid:
            raise ValueError(
                f"{paper_size} is outside the capabilities of {input_source}. "
                f"ScanRegion width: {min_width}..{max_width or 'unbounded'}, "
                f"height: {min_height}..{max_height or 'unbounded'}; "
                f"page width: {min_page_width}..{max_page_width or 'unbounded'}, "
                f"height: {min_page_height}..{max_page_height or 'unbounded'}."
            )

        return paper_size, width, height

    @staticmethod
    def _select_resolution(requested: int, caps: dict) -> int:
        supported = caps.get("resolutions", [])
        ranges = caps.get("resolution_ranges", [])

        if requested in supported:
            return requested

        candidates = list(supported)
        for item in ranges:
            lower = item["min"]
            upper = item["max"]
            candidates.append(min(max(requested, lower), upper))

        if not candidates:
            raise ValueError(
                "Scanner did not advertise supported resolutions or resolution ranges."
            )

        lower_or_equal = [value for value in candidates if value <= requested]
        selected = max(lower_or_equal) if lower_or_equal else min(candidates)

        logger.warning(
            "Requested resolution %s is unsupported. Using %s instead. "
            "Discrete values: %s; ranges: %s",
            requested, selected, supported, ranges,
        )
        return selected

    @staticmethod
    def _select_color_mode(requested: str, caps: dict) -> str:
        supported = caps.get("color_modes", [])

        if not supported:
            raise ValueError(
                "Scanner did not advertise supported color modes."
            )

        if requested in supported:
            return requested

        selected = (
            "RGB24"
            if requested == "RGB24" and "RGB24" in supported
            else supported[0]
        )

        logger.warning(
            "Color mode %s is unsupported. Using %s instead. "
            "Supported: %s",
            requested,
            selected,
            supported,
        )

        return selected

    @staticmethod
    def _select_format(requested: str, caps: dict) -> tuple:
        supported_mimes = caps.get("doc_formats", [])
        requested_mime = SHORTCUT_FORMAT_TO_MIME.get(str(requested).lower(), str(requested))

        if requested_mime in supported_mimes:
            selected_mime = requested_mime
        else:
            if "application/pdf" in supported_mimes:
                selected_mime = "application/pdf"
            elif supported_mimes:
                selected_mime = supported_mimes[0]
            else:
                selected_mime = "application/pdf"

            logger.warning(
                "Requested format '%s' (%s) is unsupported by scanner capabilities %s. Using '%s' instead.",
                requested, requested_mime, supported_mimes, selected_mime,
            )

        pil_format, ext = MIME_MAP.get(selected_mime, ("PDF", ".pdf"))
        return selected_mime, pil_format, ext

    def _resolve_location(self, location: str) -> str:
        """Resolve an absolute or relative Location header safely."""
        return urljoin(self.printer_url + "/", location)

    async def register_subscription(self) -> bool:
        url = f"{self.printer_url}/eSCL/WalkupSubscriptions"

        shortcuts_xml = "".join(
            f"<Shortcut>{escape(str(name))}</Shortcut>"
            for name in self.shortcuts
        )

        body = f"""<?xml version="1.0" encoding="UTF-8"?>
<WalkupScanSubscription xmlns="{NS_ESCL}">
    <HostIdentifier>{escape(str(self.host_id))}</HostIdentifier>
    <Connection>Network</Connection>
    <Shortcuts>
        {shortcuts_xml}
    </Shortcuts>
</WalkupScanSubscription>"""

        logger.info("Registering walkup subscription for '%s'...", self.host_id)
        headers = {
            "Content-Type": "text/xml; charset=utf-8",
            "Cache-Control": "no-cache",
        }

        try:
            response = await self.client.post(url, content=body, headers=headers)
            if response.status_code == 201 and response.headers.get("Location"):
                self.subscription_url = self._resolve_location(
                    response.headers["Location"]
                )
                path = urlsplit(self.subscription_url).path.rstrip("/")
                self.subscription_id = unquote(path.rsplit("/", 1)[-1])

                if not self.subscription_id:
                    logger.error("Printer returned an invalid subscription Location.")
                    self.subscription_url = None
                    return False

                logger.info(
                    "Successfully subscribed. Subscription ID: %s",
                    self.subscription_id,
                )
                return True

            logger.error(
                "Failed to subscribe: HTTP %s\n%s",
                response.status_code,
                response.text[:1000],
            )
            return False

        except httpx.HTTPError as exc:
            logger.warning("Printer offline or unreachable during subscription: %s", exc)
            return False
        except Exception:
            logger.exception("Unexpected error during subscription")
            return False

    async def delete_subscription(self):
        if not self.subscription_url:
            return

        logger.info("Cleaning up subscription on printer...")
        try:
            response = await self.client.delete(self.subscription_url)
            if response.status_code not in (200, 202, 204, 404, 410):
                logger.warning(
                    "Subscription cleanup returned HTTP %s: %s",
                    response.status_code,
                    response.text[:500],
                )
            else:
                logger.info("Subscription removed.")
        except httpx.HTTPError as exc:
            logger.warning("Error removing subscription: %s", exc)
        finally:
            self.subscription_url = None
            self.subscription_id = None

    async def check_scanner_status(self) -> str:
        url = f"{self.printer_url}/eSCL/ScannerStatus"

        try:
            response = await self.client.get(url)
            response.raise_for_status()
            root = ET.fromstring(response.content)
        except (httpx.HTTPError, ET.ParseError) as exc:
            raise RuntimeError(f"Could not read scanner status: {exc}") from exc

        adf_state = find_text(root, "AdfState")
        if adf_state == "ScannerAdfLoaded":
            return "Feeder"

        return "Platen"

    def _get_shortcut_config(self, shortcut_name: str) -> dict:
        if shortcut_name in self.shortcuts:
            return self.shortcuts[shortcut_name]

        lowered_name = shortcut_name.casefold()
        matches = [
            (key, value)
            for key, value in self.shortcuts.items()
            if str(key).casefold() in lowered_name
        ]
        if matches:
            return max(matches, key=lambda item: len(str(item[0])))[1]

        logger.warning(
            "No config found for shortcut '%s'. Using default settings.",
            shortcut_name,
        )
        return {
            "duplex": False,
            "resolution": 300,
            "color_mode": "RGB24",
            "format": "pdf",
            "output_dir": "/consume",
        }

    def _build_scan_settings(
        self,
        input_source: str,
        shortcut_cfg: dict,
        caps: dict,
    ):
        requested_resolution = int(
            shortcut_cfg.get("resolution", 300)
        )

        if requested_resolution <= 0:
            raise ValueError(
                "Scan resolution must be a positive integer."
            )

        resolution = self._select_resolution(
            requested_resolution,
            caps,
        )

        color_mode = self._select_color_mode(
            str(shortcut_cfg.get("color_mode", "RGB24")),
            caps,
        )

        escl_mime, pil_format, ext = self._select_format(
            shortcut_cfg.get("format", "pdf"), caps
        )

        paper_size, paper_width, paper_height = (
            self._select_paper_size(
                shortcut_cfg,
                input_source,
                caps,
            )
        )

        duplex = bool(shortcut_cfg.get("duplex", False))

        if duplex and input_source != "Feeder":
            raise ValueError("Duplex scanning is available only through the ADF.")
        if duplex and not caps.get("supports_duplex", False):
            raise ValueError(
                "Duplex was requested, but ScannerCapabilities does not advertise "
                "AdfDuplexInputCaps for this printer."
            )

        duplex_xml = (
            f"<Duplex>{'true' if duplex else 'false'}</Duplex>"
            if input_source == "Feeder"
            else ""
        )

        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<ScanSettings xmlns="{NS_ESCL}"
            xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
    <Version xmlns="{NS_PWG}">2.9</Version>
    <ContextID>{escape(str(self.subscription_id or ""))}</ContextID>
    <ScanRegions xmlns="{NS_PWG}" xmlns:n0="{NS_PWG}" n0:MustHonor="false">
        <ScanRegion>
            <Height>{paper_height}</Height>
            <Width>{paper_width}</Width>
            <XOffset>0</XOffset>
            <YOffset>0</YOffset>
            <ContentRegionUnits>escl:ThreeHundredthsOfInches</ContentRegionUnits>
        </ScanRegion>
    </ScanRegions>
    <DocumentFormatExt>{escl_mime}</DocumentFormatExt>
    <InputSource xmlns="{NS_PWG}">{input_source}</InputSource>
    <XResolution>{resolution}</XResolution>
    <YResolution>{resolution}</YResolution>
    <ColorMode>{escape(color_mode)}</ColorMode>
    {duplex_xml}
    <Brightness>4</Brightness>
    <CompressionFactor>50</CompressionFactor>
    <Contrast>4</Contrast>
    <JobSourceInfo>
        <UserName>LOCAL SERVICE</UserName>
        <UserDomain></UserDomain>
        <MachineName>{escape(str(self.host_id))}</MachineName>
        <AppFileName>svchost.exe</AppFileName>
        <Application>WIA client</Application>
    </JobSourceInfo>
</ScanSettings>"""

        return xml, {
            "paper_size": paper_size,
            "paper_width": paper_width,
            "paper_height": paper_height,
            "resolution": resolution,
            "color_mode": color_mode,
            "escl_mime": escl_mime,
            "pil_format": pil_format,
            "ext": ext,
            "duplex": duplex,
        }

    async def handle_scan_request(self, shortcut_name: str):
        shortcut_cfg = self._get_shortcut_config(shortcut_name)

        if not isinstance(shortcut_cfg, dict):
            logger.error("Shortcut '%s' configuration must be an object.", shortcut_name)
            return

        try:
            input_source = await self.check_scanner_status()
            all_caps = await self.get_scanner_capabilities()

            duplex_requested = bool(shortcut_cfg.get("duplex", False))
            if input_source == "Feeder":
                caps_key = "FeederDuplex" if duplex_requested else "FeederSimplex"
                if caps_key not in all_caps:
                    if duplex_requested:
                        raise ValueError(
                            "Duplex was requested, but this printer does not advertise "
                            "AdfDuplexInputCaps. Disable duplex in this shortcut or use "
                            "a duplex-capable ADF."
                        )
                    raise ValueError(
                        "The printer does not advertise AdfSimplexInputCaps."
                    )
                source_caps = all_caps[caps_key]
            else:
                if duplex_requested:
                    raise ValueError(
                        "Duplex was requested, but no document is loaded in the ADF. "
                        "Duplex scanning cannot use the platen."
                    )
                if "Platen" not in all_caps:
                    raise ValueError(
                        "The printer does not advertise PlatenInputCaps."
                    )
                source_caps = all_caps["Platen"]

            scan_settings_xml, selected = self._build_scan_settings(
                input_source, shortcut_cfg, source_caps
            )

            effective_shortcut_cfg = dict(shortcut_cfg)
            effective_shortcut_cfg.update(selected)
            effective_shortcut_cfg["_input_source"] = input_source

            if (
                self.current_session_documents
                and self.current_shortcut_cfg != effective_shortcut_cfg
            ):
                await self.finalize_session()

        except Exception as exc:
            logger.error(
                "Cannot prepare scan request '%s': %s",
                shortcut_name,
                exc,
            )
            return

        logger.info(
            "Submitting ScanJob (%s, Source: %s, Paper: %s, "
            "Resolution: %s dpi, Color: %s)",
            shortcut_name,
            input_source,
            selected["paper_size"],
            selected["resolution"],
            selected["color_mode"],
        )

        jobs_url = f"{self.printer_url}/eSCL/ScanJobs"
        headers = {"Content-Type": "text/xml; charset=utf-8"}

        try:
            response = await self.client.post(
                jobs_url, content=scan_settings_xml, headers=headers
            )
        except httpx.HTTPError as exc:
            logger.error("Network error creating scan job: %s", exc)
            return

        if response.status_code != 201 or not response.headers.get("Location"):
            logger.error(
                "Failed to create scan job: HTTP %s\n%s",
                response.status_code,
                response.text[:1000],
            )
            return

        job_url = self._resolve_location(response.headers["Location"])
        next_document_url = f"{job_url.rstrip('/')}/NextDocument"
        page_num = 1

        while self.running:
            document_path = None
            try:
                async with self.client.stream("GET", next_document_url) as document_response:
                    status_code = document_response.status_code
                    content_type = document_response.headers.get("Content-Type", "unknown")
                    content_location = document_response.headers.get("Content-Location", "")

                    if status_code == 200:
                        if self.session_temp_dir is None:
                            self.session_temp_dir = Path(tempfile.mkdtemp(prefix="hp-scan-session-"))
                        fd, path_string = tempfile.mkstemp(
                            prefix=f"document_{page_num:04d}_",
                            suffix=".bin",
                            dir=self.session_temp_dir,
                        )
                        os.close(fd)
                        document_path = Path(path_string)
                        downloaded_bytes = 0
                        with document_path.open("wb") as output_file:
                            async for chunk in document_response.aiter_bytes(64 * 1024):
                                output_file.write(chunk)
                                downloaded_bytes += len(chunk)
                    else:
                        await document_response.aread()
                        error_preview = document_response.text[:500]

            except httpx.HTTPError as exc:
                logger.error("Network error fetching page %s: %s", page_num, exc)
                if document_path and document_path.exists():
                    document_path.unlink(missing_ok=True)
                break

            if status_code == 503:
                if document_path and document_path.exists():
                    document_path.unlink(missing_ok=True)
                await asyncio.sleep(1)
                continue

            if status_code == 200:
                try:
                    actual_format = self._detect_document_format(document_path, content_type)
                    if actual_format == "pdf":
                        document_pages = self._pdf_page_count(document_path)
                    else:
                        document_pages = self._image_page_count(document_path)
                except Exception as exc:
                    with document_path.open("rb") as source_file:
                        preview = source_file.read(120)
                    logger.error(
                        "Printer returned unreadable data for NextDocument #%s: %s. "
                        "Content-Type=%s, bytes=%s, first_32_bytes=%s, preview=%r",
                        page_num, exc, content_type, downloaded_bytes,
                        preview[:32].hex(" "), preview,
                    )
                    document_path.unlink(missing_ok=True)
                    break

                requested_mime = selected["escl_mime"]
                expected_format = MIME_TO_FORMAT.get(requested_mime)
                if expected_format and actual_format != expected_format:
                    logger.warning(
                        "Printer returned %s although %s was requested. "
                        "Processing the actual response format.",
                        actual_format.upper(),
                        requested_mime,
                    )

                if not self.current_shortcut_cfg:
                    self.current_shortcut_cfg = dict(effective_shortcut_cfg)

                self.current_session_documents.append({
                    "path": document_path,
                    "format": actual_format,
                    "content_type": content_type,
                    "content_location": content_location,
                    "pages": document_pages,
                })
                logger.info(
                    "Downloaded NextDocument #%s: format=%s, Content-Type=%s, "
                    "pages=%s, bytes=%s%s",
                    page_num,
                    actual_format,
                    content_type,
                    document_pages,
                    downloaded_bytes,
                    f", Content-Location={content_location}" if content_location else "",
                )
                page_num += 1
                continue

            if status_code == 404:
                logger.info("All pages for this ScanJob have been received.")
                break

            logger.error(
                "Unexpected NextDocument response: HTTP %s\n%s",
                status_code,
                error_preview,
            )
            break

        logger.info(
            "Total response documents collected in active session: %s",
            len(self.current_session_documents),
        )

    @staticmethod
    def _detect_document_format(path: Path, content_type: str) -> str:
        """Detect the actual payload type from a disk-backed response."""
        if not path.is_file() or path.stat().st_size == 0:
            raise ValueError("NextDocument response body is empty.")

        with path.open("rb") as source:
            header = source.read(1024)
        if b"%PDF-" in header:
            return "pdf"

        try:
            with Image.open(path) as image:
                image_format = (image.format or "").upper()
                image.verify()
        except Exception as exc:
            normalized_type = content_type.split(";", 1)[0].strip().lower()
            raise ValueError(
                f"Payload is neither a readable PDF nor a supported image "
                f"(HTTP Content-Type={normalized_type or 'unknown'}): {exc}"
            ) from exc

        format_map = {"JPEG": "jpeg", "JPG": "jpeg", "PNG": "png", "TIFF": "tiff"}
        actual_format = format_map.get(image_format)
        if not actual_format:
            raise ValueError(
                f"Unsupported image format {image_format or 'unknown'} "
                f"(HTTP Content-Type={content_type})."
            )
        return actual_format

    @staticmethod
    def _pdf_page_count(path: Path) -> int:
        try:
            reader = PdfReader(str(path), strict=False)
            if reader.is_encrypted and not reader.decrypt(""):
                raise ValueError("PDF is encrypted and has no empty-password access.")
            count = len(reader.pages)
            if count < 1:
                raise ValueError("PDF contains no pages.")
            return count
        except Exception as exc:
            raise ValueError(f"Invalid PDF payload: {exc}") from exc

    @staticmethod
    def _image_page_count(path: Path) -> int:
        try:
            with Image.open(path) as image:
                return int(getattr(image, "n_frames", 1))
        except Exception as exc:
            raise ValueError(f"Invalid image payload: {exc}") from exc

    @staticmethod
    def _load_image_frames(document: dict) -> list:
        """Decode frames from a disk-backed raster response."""
        images = []
        try:
            with Image.open(document["path"]) as source:
                frame_count = int(getattr(source, "n_frames", 1))
                for frame_index in range(frame_count):
                    source.seek(frame_index)
                    source.load()
                    images.append(source.copy())
        except Exception as exc:
            for image in images:
                image.close()
            raise ValueError(
                f"Could not decode {document.get('format', 'image')} response: {exc}"
            ) from exc
        return images

    @staticmethod
    def _make_temp_path(directory: Path, prefix: str, suffix: str) -> Path:
        fd, path = tempfile.mkstemp(prefix=prefix, suffix=suffix, dir=directory)
        os.close(fd)
        return Path(path)

    @staticmethod
    def _prepare_pdf_image(image: Image.Image) -> Image.Image:
        if image.mode in ("1", "L", "RGB"):
            return image.copy()
        return image.convert("RGB")

    def _image_document_to_pdf(self, document: dict, resolution: int) -> tuple[Path, int]:
        images = self._load_image_frames(document)
        pdf_images = []
        output_path = self._make_temp_path(self.session_temp_dir, "converted_", ".pdf")
        try:
            for image in images:
                pdf_images.append(self._prepare_pdf_image(image))
            if not pdf_images:
                raise ValueError("Image response contains no frames.")
            pdf_images[0].save(
                output_path, format="PDF", save_all=True,
                append_images=pdf_images[1:], resolution=int(resolution),
            )
            return output_path, len(pdf_images)
        finally:
            for image in images + pdf_images:
                try:
                    image.close()
                except Exception:
                    pass

    def _documents_to_pdf(self, documents: list, resolution: int) -> tuple[Path, int]:
        """Create a PDF on disk, avoiding an in-memory copy of the final document."""
        if len(documents) == 1 and documents[0]["format"] == "pdf":
            path = Path(documents[0]["path"])
            return path, self._pdf_page_count(path)

        writer = PdfWriter()
        readers = []
        total_pages = 0
        output_path = self._make_temp_path(self.session_temp_dir, "merged_", ".pdf")
        try:
            for document_index, document in enumerate(documents, start=1):
                if document["format"] == "pdf":
                    pdf_path = Path(document["path"])
                else:
                    pdf_path, _ = self._image_document_to_pdf(document, resolution)

                reader = PdfReader(str(pdf_path), strict=False)
                if reader.is_encrypted and not reader.decrypt(""):
                    raise ValueError(
                        f"PDF response {document_index} is encrypted and cannot be opened."
                    )
                readers.append(reader)
                for page in reader.pages:
                    writer.add_page(page)
                    total_pages += 1

            if total_pages < 1:
                raise ValueError("No pages were available to write to the PDF.")
            with output_path.open("wb") as output:
                writer.write(output)
            return output_path, total_pages
        finally:
            writer.close()

    def _atomic_copy(self, output_dir: Path, filename: str, source_path: Path) -> Path:
        output_dir.mkdir(parents=True, exist_ok=True)
        target_path = output_dir / filename
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", suffix=".tmp", prefix=".scan_",
                dir=output_dir, delete=False,
            ) as temp_file:
                temp_path = Path(temp_file.name)
                with Path(source_path).open("rb") as source_file:
                    shutil.copyfileobj(source_file, temp_file, length=1024 * 1024)
                temp_file.flush()
                os.fsync(temp_file.fileno())
            os.replace(temp_path, target_path)
            temp_path = None
            try:
                os.chown(target_path, self.puid, self.pgid)
            except (PermissionError, OSError) as exc:
                logger.warning(
                    "Could not set file ownership to UID:GID %s:%s: %s",
                    self.puid, self.pgid, exc,
                )
            return target_path
        finally:
            if temp_path and temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    logger.warning("Could not remove temporary file: %s", temp_path)

    def save_documents(self, documents: list, shortcut_cfg: dict):
        """Save the collected eSCL documents in the configured output format."""
        if not documents:
            raise ValueError("No scan documents were collected.")

        output_dir = Path(shortcut_cfg.get("output_dir", "/consume"))
        output_dir.mkdir(parents=True, exist_ok=True)

        pil_format = str(shortcut_cfg.get("pil_format", "PDF")).upper()
        ext = shortcut_cfg.get("ext", ".pdf")
        resolution = int(shortcut_cfg.get("resolution", 300))
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        file_id = uuid.uuid4().hex[:6]
        formats = {document["format"] for document in documents}

        if pil_format == "PDF":
            pdf_path, page_count = self._documents_to_pdf(documents, resolution)
            filename = f"scan_{timestamp}_{file_id}{ext}"
            target_path = self._atomic_copy(output_dir, filename, pdf_path)
            logger.info("PDF saved successfully: %s (%s page(s))", target_path, page_count)
            return target_path, page_count

        if "pdf" in formats:
            raise ValueError(
                f"Printer returned PDF but shortcut requests {pil_format}; "
                "automatic PDF-to-image conversion is not enabled. "
                "Set this shortcut's format to 'pdf' to preserve the scan."
            )

        all_images = []
        try:
            for document in documents:
                all_images.extend(self._load_image_frames(document))

            if not all_images:
                raise ValueError("No raster pages were available to save.")

            if pil_format == "TIFF":
                filename = f"scan_{timestamp}_{file_id}{ext}"
                temp_output = self._make_temp_path(self.session_temp_dir, "output_", ".tif")
                all_images[0].save(
                    temp_output, format="TIFF", save_all=True,
                    append_images=all_images[1:], resolution=resolution,
                )
                target_path = self._atomic_copy(output_dir, filename, temp_output)
                logger.info("TIFF saved successfully: %s (%s page(s))", target_path, len(all_images))
                return target_path, len(all_images)

            if pil_format not in ("JPEG", "PNG"):
                raise ValueError(f"Unsupported output image format: {pil_format}")

            saved_paths = []
            for index, image in enumerate(all_images, start=1):
                output_image = image
                converted = None
                try:
                    if pil_format == "JPEG" and image.mode not in ("L", "RGB"):
                        converted = image.convert("RGB")
                        output_image = converted

                    suffix = f"_p{index}" if len(all_images) > 1 else ""
                    filename = f"scan_{timestamp}_{file_id}{suffix}{ext}"
                    temp_output = self._make_temp_path(self.session_temp_dir, "output_", ext)
                    output_image.save(temp_output, format=pil_format, resolution=resolution)
                    target_path = self._atomic_copy(output_dir, filename, temp_output)
                    saved_paths.append(target_path)
                    logger.info("Image saved successfully: %s", target_path)
                finally:
                    if converted is not None:
                        converted.close()

            return saved_paths[0], len(saved_paths)
        finally:
            for image in all_images:
                try:
                    image.close()
                except Exception:
                    pass

    async def finalize_session(self):
        if not self.current_session_documents:
            self.current_shortcut_cfg = None
            temp_dir = self.session_temp_dir
            self.session_temp_dir = None
            if temp_dir and temp_dir.exists():
                await asyncio.to_thread(shutil.rmtree, temp_dir, True)
            return

        logger.info(
            "Finalizing session. Processing %s response document(s)...",
            len(self.current_session_documents),
        )

        documents = self.current_session_documents
        cfg = self.current_shortcut_cfg or {
            "resolution": 300,
            "output_dir": "/consume",
            "pil_format": "PDF",
            "ext": ".pdf",
        }

        self.current_session_documents = []
        self.current_shortcut_cfg = None
        self.session_temp_dir = None

        try:
            output_path, page_count = await asyncio.to_thread(
                self.save_documents, documents, cfg
            )
            logger.info("Scan output saved: %s (%s page(s))", output_path, page_count)
        except Exception:
            logger.exception("Failed to save the current scan session")
        finally:
            temp_dir = self.session_temp_dir
            self.session_temp_dir = None
            if temp_dir and temp_dir.exists():
                await asyncio.to_thread(shutil.rmtree, temp_dir, True)

    async def poll_events(self):
        if not self.subscription_url:
            return

        event_url = f"{self.subscription_url}/Event?timeout=100"

        while self.running:
            try:
                response = await self.client.get(event_url, timeout=110.0)

                if response.status_code == 204:
                    continue

                if response.status_code == 200:
                    try:
                        root = ET.fromstring(response.content)
                    except ET.ParseError as exc:
                        logger.warning("Ignoring malformed event XML: %s", exc)
                        continue

                    event_type = find_text(root, "Event") or ""
                    if event_type == "HostSelected":
                        logger.info("Host selected on printer panel.")
                        if self.current_session_documents:
                            await self.finalize_session()

                    elif event_type == "ScanRequested":
                        shortcut_name = find_text(root, "Shortcut") or "paperless"
                        logger.info("Scan requested! Shortcut: '%s'", shortcut_name)
                        await self.handle_scan_request(shortcut_name)

                    elif event_type == "ScanPagesComplete":
                        logger.info(
                            "ScanPagesComplete event received. Finalizing batch PDF..."
                        )
                        await self.finalize_session()

                    else:
                        logger.debug("Received eSCL event: %s", event_type)

                    continue

                if response.status_code in (404, 410):
                    logger.warning(
                        "Subscription expired or printer restarted (HTTP %s). "
                        "Re-registering subscription...",
                        response.status_code,
                    )
                    break

                logger.warning("Unexpected event response: HTTP %s", response.status_code)
                await asyncio.sleep(2)

            except httpx.TimeoutException:
                continue
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.NetworkError) as exc:
                logger.warning(
                    "Printer offline or unreachable: %s. Will retry subscription in 10s...",
                    exc,
                )
                await asyncio.sleep(10)
                break
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Error during long-poll")
                await asyncio.sleep(5)

    async def run(self):
        while self.running:
            subscribed = await self.register_subscription()

            if subscribed:
                await self.poll_events()
            elif self.running:
                logger.warning(
                    "Subscription failed or printer offline. Retrying in 15 seconds..."
                )
                await asyncio.sleep(15)

    async def stop(self):
        self.running = False
        await self.finalize_session()
        await self.delete_subscription()
        await self.client.aclose()


async def main():
    scanner = ESCLWalkupScanner("config.json")
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    run_task = asyncio.create_task(scanner.run())

    try:
        await stop_event.wait()
    finally:
        run_task.cancel()
        await asyncio.gather(run_task, return_exceptions=True)
        await scanner.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
