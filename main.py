import asyncio
import datetime
import io
import json
import logging
import os
import signal
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
import httpx
from PIL import Image

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("escl-walkup")

NS_ESCL = "http://schemas.hp.com/imaging/escl/2011/05/03"
NS_PWG = "http://www.pwg.org/schemas/2010/12/sm"


class ESCLWalkupScanner:
    def __init__(self, config_path: str = "config.json"):
        with open(config_path, "r", encoding="utf-8") as f:
            self.config = json.load(f)

        self.printer_url = self.config["printer_url"].rstrip("/")
        self.host_id = self.config.get("host_identifier", "Homelab-Scanner")
        self.verify_ssl = self.config.get("verify_ssl", False)
        self.puid = int(self.config.get("puid", 1000))
        self.pgid = int(self.config.get("pgid", 1000))
        self.shortcuts = self.config.get("shortcuts", {})

        self.client = httpx.AsyncClient(
            verify=self.verify_ssl,
            timeout=httpx.Timeout(120.0, connect=10.0),
        )
        self.subscription_url = None
        self.subscription_id = None
        self.running = True

        self.current_session_pages = []
        self.current_shortcut_cfg = None

    async def register_subscription(self) -> bool:
        url = f"{self.printer_url}/eSCL/WalkupSubscriptions"
        shortcuts_xml = "".join(
            f"<Shortcut>{name}</Shortcut>" for name in self.shortcuts.keys()
        )
        body = f"""<?xml version="1.0" encoding="UTF-8"?>
<WalkupScanSubscription xmlns="{NS_ESCL}">
	<HostIdentifier>{self.host_id}</HostIdentifier>
	<Connection>Network</Connection>
	<Shortcuts>
		{shortcuts_xml}
	</Shortcuts>
</WalkupScanSubscription>"""

        logger.info(f"Registering walkup subscription for '{self.host_id}'...")
        headers = {"Content-Type": "text/xml", "Cache-Control": "no-cache"}
        try:
            resp = await self.client.post(url, content=body, headers=headers)
            if resp.status_code == 201 and "Location" in resp.headers:
                loc = resp.headers["Location"]
                self.subscription_url = loc if loc.startswith("http") else f"{self.printer_url}{loc}"
                self.subscription_id = self.subscription_url.split("/")[-1]
                logger.info(f"Successfully subscribed. Subscription ID: {self.subscription_id}")
                return True
            else:
                logger.error(f"Failed to subscribe: HTTP {resp.status_code} - {resp.text}")
                return False
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.NetworkError) as e:
            logger.warning(f"Printer offline or unreachable during subscription: {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error during subscription: {e}")
            return False

    async def delete_subscription(self):
        if self.subscription_url:
            logger.info("Cleaning up subscription on printer...")
            try:
                await self.client.delete(self.subscription_url)
                logger.info("Subscription removed.")
            except Exception as e:
                logger.warning(f"Error removing subscription: {e}")

    async def check_scanner_status(self) -> str:
        url = f"{self.printer_url}/eSCL/ScannerStatus"
        try:
            resp = await self.client.get(url)
            if resp.status_code == 200:
                root = ET.fromstring(resp.text)
                adf_elem = root.find(f"{{{NS_ESCL}}}AdfState")
                if adf_elem is not None and adf_elem.text == "ScannerAdfLoaded":
                    return "Feeder"
        except Exception as e:
            logger.warning(f"Failed to fetch status: {e}")
        return "Platen"

    async def handle_scan_request(self, shortcut_name: str, settings_elem: ET.Element):
        shortcut_cfg = self.shortcuts.get(shortcut_name)
        if not shortcut_cfg:
            for key, val in self.shortcuts.items():
                if key.lower() in shortcut_name.lower():
                    shortcut_cfg = val
                    break

        if not shortcut_cfg:
            logger.warning(f"No config found for shortcut '{shortcut_name}'. Using default.")
            shortcut_cfg = {
                "duplex": False,
                "resolution": 300,
                "color_mode": "RGB24",
                "output_dir": "/consume",
            }

        input_source = await self.check_scanner_status()
        res = shortcut_cfg.get("resolution", 300)
        color_mode = shortcut_cfg.get("color_mode", "RGB24")
        duplex = shortcut_cfg.get("duplex", False)

        if input_source == "Feeder":
            height = 4200
            duplex_str = "<Duplex>true</Duplex>" if duplex else "<Duplex>false</Duplex>"
        else:
            height = 3507
            duplex_str = ""

        scan_settings_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<ScanSettings xmlns="{NS_ESCL}" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:schemaLocation="{NS_ESCL} Scan Schema - 0.26.xsd">
	<Version xmlns="{NS_PWG}">2.9</Version>
	<ContextID>{self.subscription_id}</ContextID>
	<Intent>Document</Intent>
	<ScanRegions xmlns="{NS_PWG}" xmlns:n0="{NS_PWG}" n0:MustHonor="false">
		<ScanRegion>
			<Height>{height}</Height>
			<Width>2550</Width>
			<XOffset>0</XOffset>
			<YOffset>0</YOffset>
			<ContentRegionUnits>escl:ThreeHundredthsOfInches</ContentRegionUnits>
		</ScanRegion>
	</ScanRegions>
	<DocumentFormatExt>image/jpeg</DocumentFormatExt>
	<InputSource xmlns="{NS_PWG}">{input_source}</InputSource>
	<XResolution>{res}</XResolution>
	<YResolution>{res}</YResolution>
	<ColorMode>{color_mode}</ColorMode>
	{duplex_str}
	<Brightness>4</Brightness>
	<CompressionFactor>50</CompressionFactor>
	<Contrast>4</Contrast>
	<JobSourceInfo>
		<UserName>LOCAL SERVICE</UserName>
		<UserDomain></UserDomain>
		<MachineName>{self.host_id}</MachineName>
		<AppFileName>svchost.exe</AppFileName>
		<Application>WIA client</Application>
	</JobSourceInfo>
</ScanSettings>"""

        logger.info(f"Submitting ScanJob ({shortcut_name}, Source: {input_source}, Duplex: {duplex}, Height: {height})...")
        jobs_url = f"{self.printer_url}/eSCL/ScanJobs"
        headers = {"Content-Type": "text/xml"}
        try:
            resp = await self.client.post(jobs_url, content=scan_settings_xml, headers=headers)
        except Exception as e:
            logger.error(f"Network error creating scan job: {e}")
            return

        if resp.status_code != 201 or "Location" not in resp.headers:
            logger.error(f"Failed to create scan job: HTTP {resp.status_code} - {resp.text}")
            return

        job_location = resp.headers["Location"]
        job_url = job_location if job_location.startswith("http") else f"{self.printer_url}{job_location}"
        
        page_num = 1
        next_doc_url = f"{job_url}/NextDocument"

        while True:
            try:
                doc_resp = await self.client.get(next_doc_url)
                if doc_resp.status_code == 503:
                    await asyncio.sleep(1)
                    continue
                elif doc_resp.status_code == 200:
                    logger.info(f"Downloaded page {page_num} ({len(doc_resp.content)} bytes).")
                    self.current_session_pages.append(doc_resp.content)
                    if not self.current_shortcut_cfg:
                        self.current_shortcut_cfg = shortcut_cfg
                    page_num += 1
                elif doc_resp.status_code == 404:
                    logger.info("Current job pages received.")
                    break
                else:
                    logger.error(f"Unexpected document fetch status: HTTP {doc_resp.status_code}")
                    break
            except Exception as e:
                logger.error(f"Error fetching page {page_num}: {e}")
                break

        logger.info(f"Total pages collected in active session so far: {len(self.current_session_pages)}")

    async def finalize_session(self):
        if self.current_session_pages:
            logger.info(f"Finalizing session. Generating PDF for {len(self.current_session_pages)} page(s)...")
            cfg = self.current_shortcut_cfg or {
                "duplex": False,
                "resolution": 300,
                "color_mode": "RGB24",
                "output_dir": "/consume",
            }
            await self.save_pdf(self.current_session_pages, cfg)
            self.current_session_pages = []
            self.current_shortcut_cfg = None

    async def save_pdf(self, image_buffers: list[bytes], shortcut_cfg: dict):
        output_dir = Path(shortcut_cfg.get("output_dir", "/consume"))
        output_dir.mkdir(parents=True, exist_ok=True)

        pil_images = []
        for img_bytes in image_buffers:
            img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
            pil_images.append(img)

        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"scan_{timestamp}.pdf"
        target_path = output_dir / filename

        pil_images[0].save(target_path, save_all=True, append_images=pil_images[1:], format="PDF")
        logger.info(f"PDF saved successfully: {target_path}")

        try:
            os.chown(target_path, self.puid, self.pgid)
            logger.info(f"Set file permissions to UID: {self.puid}, GID: {self.pgid}")
        except Exception as e:
            logger.warning(f"Failed to set file permissions via chown: {e}")

    async def poll_events(self):
        event_url = f"{self.subscription_url}/Event?timeout=100"
        while self.running:
            try:
                resp = await self.client.get(event_url, timeout=110.0)
                if resp.status_code == 204:
                    continue
                elif resp.status_code == 200:
                    root = ET.fromstring(resp.text)
                    event_type_elem = root.find(f"{{{NS_ESCL}}}Event")
                    event_type = event_type_elem.text if event_type_elem is not None else ""

                    if event_type == "HostSelected":
                        logger.info("Host selected on printer panel.")
                        if self.current_session_pages:
                            await self.finalize_session()
                    elif event_type == "ScanRequested":
                        shortcut_elem = root.find(f"{{{NS_ESCL}}}Shortcut")
                        shortcut_name = shortcut_elem.text if shortcut_elem is not None else "paperless"
                        settings_elem = root.find(f"{{{NS_ESCL}}}ScanSettings")
                        logger.info(f"Scan requested! Shortcut: '{shortcut_name}'")
                        await self.handle_scan_request(shortcut_name, settings_elem)
                    elif event_type == "ScanPagesComplete":
                        logger.info("ScanPagesComplete event received. Finalizing batch PDF...")
                        await self.finalize_session()
                elif resp.status_code in (404, 410):
                    logger.warning(
                        f"Subscription expired or printer restarted (HTTP {resp.status_code}). "
                        "Re-registering subscription..."
                    )
                    break
                else:
                    logger.warning(f"Unexpected event response: HTTP {resp.status_code}")
                    await asyncio.sleep(2)
            except httpx.TimeoutException:
                continue
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.NetworkError) as e:
                logger.warning(f"Printer offline or unreachable: {e}. Will retry subscription in 10s...")
                await asyncio.sleep(10)
                break
            except Exception as e:
                if self.running:
                    logger.error(f"Error during long-poll: {e}")
                    await asyncio.sleep(5)

    async def run(self):
        while self.running:
            subscribed = await self.register_subscription()
            if subscribed:
                await self.poll_events()
            else:
                if self.running:
                    logger.warning("Subscription failed or printer offline. Retrying in 15 seconds...")
                    await asyncio.sleep(15)

    async def stop(self):
        self.running = False
        await self.delete_subscription()
        await self.client.aclose()


async def main():
    scanner = ESCLWalkupScanner("config.json")
    loop = asyncio.get_running_loop()

    def shutdown():
        logger.info("Shutdown signal received.")
        asyncio.create_task(scanner.stop())

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown)

    try:
        await scanner.run()
    except asyncio.CancelledError:
        pass


if __name__ == "__main__":
    asyncio.run(main())
