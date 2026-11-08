# HP Scan To Computer (Docker)

A lightweight Python service packaged in Docker that connects modern HP printers (e.g., HP OfficeJet Pro 8120e / 8122e series) to home servers and document management systems like **Paperless-ngx**.

> ⚠️ **DISCLAIMER & WARNING**
> 
> **Use at your own risk.** This project is an unofficial, community-developed utility created **strictly for personal, experimental, and testing purposes**.
> 
> - **No Warranty:** This software is provided "as is", without warranty of any kind, express or implied.
> - **No Liability:** The author(s) shall not be held liable for any claim, damages, hardware failure, data loss, network disruption, or any other liability arising from the installation, configuration, or use of this application.
> - **Not Affiliated with HP:** This tool is not affiliated with, endorsed by, or supported by HP (Hewlett-Packard).
> 
> By using this software, you acknowledge that you are using it entirely at your own discretion and risk.

It listens for "Scan to Computer" events initiated directly from the printer's touchscreen panel using the **eSCL** protocol over HTTPS, downloads scanned pages, and compiles them into PDFs saved directly to target directories.

> ⚠️ **DISCLAIMER & WARNING**
> 
> **Use at your own risk.** This project is an unofficial, community-developed utility created **strictly for personal, experimental, and testing purposes**.
> 
> - **No Warranty:** This software is provided "as is", without warranty of any kind, express or implied.
> - **No Liability:** The author(s) shall not be held liable for any claim, damages, hardware failure, data loss, network disruption, or any other liability arising from the installation, configuration, or use of this application.
> - **Not Affiliated with HP:** This tool is not affiliated with, endorsed by, or supported by HP (Hewlett-Packard).
> 
> By using this software, you acknowledge that you are using it entirely at your own discretion and risk.


---

## Table of Contents

- [Features](#features)
- [Project Structure](#project-structure)
- [Configuration (`config.json`)](#configuration-configjson)
  - [Global Parameters](#global-parameters)
  - [Shortcut Parameters](#shortcut-parameters)
  - [Supported Paper Sizes](#supported-paper-sizes)
  - [Capability Detection](#capability-detection)
  - [Important Notes](#important-notes)
- [Deployment via Docker Compose](#deployment-via-docker-compose)
  - [docker-compose.yml](#docker-composeyml)
  - [Run Service](#run-service)
- [How It Works](#how-it-works)
- [Verified Hardware](#verified-hardware)
- [License](#license)

## Features

- **Touchscreen Integration:** Registers custom scan shortcuts directly on the printer's display panel (e.g., `paperless`, `paperless duplex`).
- **ADF & Platen Support:** Automatically detects whether paper is placed in the Automatic Document Feeder (ADF) or on the flatbed glass (Platen).
- **Capability-Based Configuration:** Automatically reads the printer's `ScannerCapabilities` endpoint and detects supported input sources, resolutions, color modes, and scan dimensions.
- **Source-Specific Capabilities:** Independently handles `PlatenInputCaps`, `AdfSimplexInputCaps`, and `AdfDuplexInputCaps`. Only uses capabilities advertised by the selected source.
- **Dynamic Paper Sizes:** Supports configurable paper sizes per shortcut, validating each requested size against the selected scanner source's actual minimum and maximum dimensions.
- **Flexible Resolution Selection:** Supports discrete resolutions and resolution ranges. If a requested resolution is unavailable, the closest suitable supported resolution is selected automatically.
- **Color Mode Selection:** Detects available color modes for each input source and selects a supported mode when the requested one is unavailable.
- **Duplex Capability Detection:** Enables duplex scanning only when the printer advertises `AdfDuplexInputCaps`. Duplex requests on simplex-only scanners are rejected with a clear error.
- **Multi-Page Batching:** Handles multi-page scans by buffering pages until the printer signals that scanning is complete through the `ScanPagesComplete` event.
- **Paperless-ngx Ready:** Output PDFs are created with configurable user and group ownership (`PUID` / `PGID`) to match Paperless consume folder permissions.
- **Reliable Document Downloading:** Waits for the next document when the printer returns HTTP 503, without imposing an arbitrary retry limit.
- **Atomic PDF Output:** Writes PDFs to a temporary file and renames them after completion, preventing Paperless-ngx from processing incomplete files.
- **Asynchronous & Efficient:** Built with `httpx` and `asyncio` for low resource consumption on home server hardware (e.g., Debian homelab, Raspberry Pi).

---

## Project Structure

```text
.
├── config.example.json   # Example configuration file with scan shortcuts
├── main.py               # Main eSCL listener service
├── Dockerfile            # Container build instructions
├── docker-compose.yml    # Docker Compose / Portainer deployment
├── requirements.txt      # Python dependencies
└── .gitignore            # Git ignore rules
```

---

## Configuration (`config.json`)

Create `config.json` in your project root directory (or use `config.example.json` as a starting point):

```json
{
  "printer_url": "https://192.168.2.199:443",
  "host_identifier": "Homelab-Scanner",
  "verify_ssl": false,
  "puid": 1000,
  "pgid": 1000,
  "paper_size": "A4",
  "shortcuts": {
    "paperless": {
      "duplex": false,
      "paper_size": "A4",
      "format": "pdf",
      "resolution": 300,
      "color_mode": "RGB24",
      "output_dir": "/consume"
    },
    "paperless duplex": {
      "duplex": false,
      "paper_size": "A4",
      "format": "pdf",
      "resolution": 300,
      "color_mode": "RGB24",
      "output_dir": "/consume/double-sided"
    },
    "small documents": {
      "duplex": false,
      "paper_size": "A5",
      "format": "pdf",
      "resolution": 300,
      "color_mode": "Grayscale8",
      "output_dir": "/consume"
    },
    "photos": {
      "duplex": false,
      "paper_size": "A6",
      "format": "jpg",
      "resolution": 600,
      "color_mode": "RGB24",
      "output_dir": "/nextcloud/photos"
    }
  }
}
```

### Global Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `printer_url` | String | - | Full HTTPS URL of your printer (e.g., `https://192.168.2.199:443`). |
| `host_identifier` | String | `Homelab-Scanner` | Hostname shown on the printer touchscreen under the destination menu. |
| `verify_ssl` | Boolean | `false` | Set to `false` for self-signed certificates on local network printers. |
| `puid` / `pgid` | Integer | `1000` | Linux user and group IDs applied to generated PDF files via `chown`. |
| `paper_size` | String | `A4` | Default paper size used when a shortcut does not define its own size. |
| `shortcuts` | Object | - | Map of scan profiles displayed on the printer touchscreen. |

### Shortcut Parameters

| Option | Type | Values | Description |
|---|---|---|---|
| `duplex` | Boolean | `true`, `false` | Enables duplex scanning through the ADF, but only if the printer advertises duplex capability. |
| `paper_size` | String | See supported sizes below | Paper size for this shortcut. Overrides the global `paper_size`. |
| `resolution` | Integer | Device-dependent | Requested scan resolution in DPI. The service selects a supported value if the requested resolution is unavailable. |
| `color_mode` | String | Device-dependent | Requested color mode, such as `RGB24`, `Grayscale8`, or `BlackAndWhite1`. |
| `format` | String | `pdf`, `jpg`, `jpeg`, `tiff`,`tif` | Output format for the scanned document. must be one of the supported formats. |
| `output_dir` | String | - | Target container directory where generated PDFs are saved. |

### Supported Paper Sizes

The following predefined paper sizes are available:

| Paper Size | Width (1/300 inch) | Height (1/300 inch) |
|---|---:|---:|
| `A3` | 3508 | 4961 |
| `A4` | 2480 | 3508 |
| `A5` | 1748 | 2480 |
| `A6` | 1240 | 1748 |
| `A7` | 874 | 1240 |
| `B4` | 2953 | 4169 |
| `B5` | 2079 | 2953 |
| `B6` | 1476 | 2079 |
| `LETTER` | 2550 | 3300 |
| `LEGAL` | 2550 | 4200 |
| `EXECUTIVE` | 2175 | 3150 |
| `STATEMENT` | 1650 | 2550 |

**Important:** These are predefined sizes, not a guarantee that every scanner supports every size. The service validates the requested dimensions against the capabilities of the selected input source. For example, a paper size may be supported by the Platen but not by the ADF.

### Capability Detection

The service queries:

```text
https://<PRINTER_IP>/eSCL/ScannerCapabilities
```

It independently detects the following capability blocks:

| Capability Block | Input Source | Description |
|---|---|---|
| `PlatenInputCaps` | Platen | Flatbed scanning capabilities |
| `AdfSimplexInputCaps` | FeederSimplex | Single-sided ADF scanning |
| `AdfDuplexInputCaps` | FeederDuplex | Double-sided ADF scanning |

The service uses the advertised capabilities to determine:

- Minimum and maximum scan width and height.
- Supported discrete resolutions and resolution ranges.
- Available color modes.
- Whether duplex scanning is supported.

This means the configuration is not limited to the capabilities of the HP OfficeJet Pro 8120e series. Other eSCL-compatible scanners can use their own advertised capabilities.

### Important Notes

- The global `paper_size` is only a fallback. A shortcut-specific `paper_size` takes priority.
- Resolution and color mode are resolved separately for the selected input source.
- Unsupported paper dimensions result in an error rather than silently selecting another paper size.
- Duplex scanning is not simulated. It is enabled only when the device advertises `AdfDuplexInputCaps`.
- A shortcut named `paperless duplex` does not automatically enable duplex scanning. It can simply save scanned pages to a separate output directory.

For the HP OfficeJet Pro 8120e / 8122e series, the ADF is simplex-only. Therefore, use `"duplex": false` for this printer.

---

## Deployment via Docker Compose

### `docker-compose.yml`

```yaml
services:
  hpscan:
    build: .
    image: hpscan:latest
    container_name: hpscan
    restart: unless-stopped
    environment:
      - TZ=Europe/Berlin
    volumes:
      - ./config.json:/app/config.json:ro
      - ./consume:/consume
```

### Run Service

```bash
# Build and start container
docker compose up -d --build

# View real-time logs
docker logs -f hpscan
```

---

## How It Works

1. **Subscription Registration:** Registers the host as a Walkup destination via `POST /eSCL/WalkupSubscriptions` over HTTPS.
2. **Long-Polling Event Loop:** Listens for touchscreen interactions via `GET /eSCL/WalkupSubscriptions/{id}/Event?timeout=100`.
3. **Job Initiation:** When a shortcut is selected on the printer, the application receives a `ScanRequested` event and queries `/eSCL/ScannerStatus` to identify the active input source.
4. **Capability Detection:** Reads `/eSCL/ScannerCapabilities` and selects the appropriate capabilities for the Platen, simplex ADF, or duplex ADF.
5. **Scan Settings:** Builds the scan request using the selected shortcut's paper size, resolution, color mode, and duplex settings.
6. **Document Downloading:** Creates a scan job via `POST /eSCL/ScanJobs` and downloads scanned JPEG pages through `/eSCL/ScanJobs/{id}/NextDocument`.
7. **Page Aggregation:** Buffers downloaded pages until the printer signals `ScanPagesComplete`.
8. **PDF Generation:** Compiles the collected images into a multi-page PDF using Pillow.
9. **Output:** Saves the completed PDF to the configured output directory and applies the configured file ownership.

---

## Verified Hardware

- **HP OfficeJet Pro 8120e Series (including 8122e)**

The implementation is designed for other new HP printers which supporting the eSCL / AirScan protocol, provided they expose compatible Walkup Scan functionality.

---

## License

[MIT License](https://opensource.org/license/mit)
