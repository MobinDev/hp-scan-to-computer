# HP Scan To Computer (`hpscan`)

A lightweight Python service packaged in Docker that connects modern HP printers (e.g., HP OfficeJet Pro 8120e / 8122e series) to home servers and document management systems like **Paperless-ngx**.

It listens for "Scan to Computer" events initiated directly from the printer's touchscreen panel using the **eSCL (AirScan / Mopria)** protocol over HTTPS, downloads scanned pages, and compiles them into PDFs saved directly to target directories.

---

## Features

- **Touchscreen Integration:** Registers custom scan shortcuts directly on the printer's display panel (e.g., `paperless`, `paperless duplex`).
- **ADF & Platen Support:** Automatically detects whether paper is placed in the Automatic Document Feeder (ADF) or on the flatbed glass (Platen).
- **Multi-Page Batching:** Handles multi-page flatbed scans gracefully by buffering pages until the **Done** button is pressed on the printer panel (`ScanPagesComplete` event).
- **Paperless-ngx Ready:** Output PDFs are created with custom user and group permissions (`PUID`/`PGID`) to match Paperless consume folder ownership.
- **Auto-Resolution & Sizing:** Adjusts scan dimensions dynamically (A4 for flatbed glass, Legal for ADF) to avoid HTTP 409 firmware conflicts.
- **Asynchronous & Efficient:** Built with `httpx` and `asyncio` for low resource consumption on home server hardware (e.g., Debian homelab, Raspberry Pi).

---

## Project Structure

```text
.
├── config.json           # Active configuration file (Printer IP, shortcuts, directories)
├── config.example.json   # Configuration template
├── main.py               # Main eSCL listener service
├── Dockerfile            # Container build instructions
├── docker-compose.yml    # Docker Compose / Portainer deployment
├── requirements.txt      # Python dependencies (httpx, Pillow)
└── .gitignore            # Git ignore rules

```

---

## Configuration (`config.json`)

Create `config.json` in your project root directory (or use `config.example.json` as a starting point):

```json
{
  "printer_url": "[https://192.168.2.199:443](https://192.168.2.199:443)",
  "host_identifier": "Homelab-Scanner",
  "verify_ssl": false,
  "poll_interval_sec": 2,
  "puid": 1000,
  "pgid": 1000,
  "shortcuts": {
    "paperless": {
      "duplex": false,  # true is not tested
      "resolution": 300,
      "color_mode": "RGB24",
      "output_dir": "/consume"
    },
    "paperless duplex": {
      "duplex": false, 
      "resolution": 300,
      "color_mode": "RGB24",
      "output_dir": "/consume/double-sided"
    }
  }
}

```

### Configuration Parameters

| Option | Type | Description |
| --- | --- | --- |
| `printer_url` | String | Full HTTPS URL of your HP printer (e.g., `https://192.168.2.199:443`). |
| `host_identifier` | String | Name displayed under "Scan to Computer" destinations on the printer panel. |
| `verify_ssl` | Boolean | Set to `false` for self-signed certificates issued by local printers. |
| `puid` / `pgid` | Integer | Linux User ID and Group ID applied to generated PDF files via `chown`. |
| `shortcuts` | Object | Dictionary of shortcuts shown on the printer display with target output paths. |

---

## Deployment via Docker Compose

### `docker-compose.yml`

```yaml
version: "3.8"

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
      - /opt/projects/paperless/consume:/consume

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

1. **Subscription Registration:** Registers host as a Walkup destination via `POST /eSCL/WalkupSubscriptions` over HTTPS.
2. **Long-Polling Event Loop:** Listens for touchscreen interactions via `GET /eSCL/WalkupSubscriptions/{id}/Event?timeout=100`.
3. **Job Initiation:** When a shortcut is selected on the printer, the app receives a `ScanRequested` event, queries `/eSCL/ScannerStatus`, and posts a scan job (`POST /eSCL/ScanJobs`).
4. **Document Downloading:** Downloads scanned JPEG pages chunk-by-chunk via `/eSCL/ScanJobs/{id}/NextDocument`.
5. **Page Aggregation & Output:** Buffers downloaded pages until the printer signals `ScanPagesComplete`, then compiles all pages into a multi-page PDF using PIL (`Pillow`) and sets file ownership.

---

## Verified Hardware

* **HP OfficeJet Pro 8120e Series (8122e)**
* Any HP printer supporting eSCL 2.9 / Mopria 1.5 AirScan over HTTPS.

---

## License

[MIT License](https://www.google.com/search?q=LICENSE)

