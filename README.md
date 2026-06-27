# FFmpeg Audio → MP3 API

A small, containerized HTTP service that wraps FFmpeg for n8n.
Send it an **audio URL**, it downloads the file, converts it to a **real MP3**
(`libmp3lame`, **128 kbps** by default) and returns a **usable public MP3 URL**
(or the raw MP3 file). Interactive **Swagger UI** is built in.

## Quick start

```bash
cp .env.example .env        # optional: adjust PUBLIC_BASE_URL / HOST_PORT
docker compose up -d --build
```

- Swagger UI: <http://localhost:8000/docs>
- Health:     <http://localhost:8000/health>

## Endpoints

| Method | Path               | Purpose                                        |
|--------|--------------------|------------------------------------------------|
| POST   | `/convert`         | Variant 1 — convert an audio **URL** (JSON)    |
| POST   | `/convert/upload`  | Variant 2 — convert an **uploaded file** (form)|
| GET    | `/files/{name}.mp3`| Download a converted MP3 (the public URL)      |
| GET    | `/health`          | Health check                                   |
| GET    | `/docs`            | Swagger UI                                      |

Every conversion produces a **genuine MP3** — codec `libmp3lame`, **128 kbps**,
served as `Content-Type: audio/mpeg` (re-encoded by FFmpeg, never a rename).

### Variant 1 — POST /convert (audio via URL)

Request body:

```json
{
  "audio_url": "https://example.com/voice.wav",
  "bitrate": "128k",
  "return_type": "url"
}
```

- `audio_url` (required) — public URL of the source audio. The key `url` is also
  accepted as an alias.
- `bitrate` (optional, default `128k`) — e.g. `128k`, `192k`, `320k`.
- `return_type` (optional, default `url`):
  - `url`  → JSON with a public MP3 link.
  - `file` → the raw MP3 bytes in the response.

### Variant 2 — POST /convert/upload (audio as a file)

`multipart/form-data` with fields:

- `file` (required) — the audio file itself.
- `bitrate` (optional, default `128k`).
- `return_type` (optional, default `url`).

```bash
curl -X POST http://localhost:8000/convert/upload \
  -F "file=@voice.ogg" -F "return_type=url"
```

Response (`return_type: url`, both variants):

```json
{
  "id": "aae23e800b2b41418381a487907fddec",
  "filename": "aae23e800b2b41418381a487907fddec.mp3",
  "mp3_url": "http://ffmpeg-api:8000/files/aae23e800b2b41418381a487907fddec.mp3",
  "size_bytes": 48944,
  "bitrate": "128k"
}
```

## Using it from n8n

n8n and this service should share a Docker network. In an **HTTP Request** node:

**If you have an audio URL:**

- Method: `POST`
- URL: `http://ffmpeg-api:8000/convert`
- Body (JSON): `{ "audio_url": "{{ $json.audio_url }}" }`

**If you have the audio file (binary) in n8n:**

- Method: `POST`
- URL: `http://ffmpeg-api:8000/convert/upload`
- Body: `Form-Data` → field `file`, type *n8n Binary File*, pointing at your
  binary property.

Either way, read `{{ $json.mp3_url }}` from the response — n8n can fetch that URL
directly (it resolves to the same service over the Docker network).

If n8n runs in a **different compose project**, attach this service to that
network — see the commented block at the bottom of `docker-compose.yml`, or set
`PUBLIC_BASE_URL` to your public domain if you expose it through a reverse proxy.

## Persistence & restarts

- `restart: unless-stopped` — the container comes back automatically after a
  container or server/Docker restart.
- Converted MP3s are written to the named volume `ffmpeg_data` (mounted at
  `/data`), so previously converted files remain downloadable after a restart.

## Configuration (env vars)

| Variable            | Default                  | Meaning                                            |
|---------------------|--------------------------|----------------------------------------------------|
| `HOST_PORT`         | `8000`                   | Host port the API is published on.                 |
| `PUBLIC_BASE_URL`   | `http://ffmpeg-api:8000` | Base used to build the returned `mp3_url`.         |
| `DEFAULT_BITRATE`   | `128k`                   | Default MP3 bitrate.                               |
| `MAX_DOWNLOAD_BYTES`| `524288000` (500 MB)     | Reject source files larger than this.              |
| `DOWNLOAD_TIMEOUT`  | `120`                    | Source download timeout (seconds).                 |

## Test proof (curl)

```bash
# 1. convert
curl -s -X POST http://localhost:8000/convert \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://download.samplelib.com/wav/sample-3s.wav"}'

# 2. fetch the returned mp3_url and confirm it is a real 128 kbps MP3
ffprobe -show_entries format=format_name,bit_rate -of default=noprint_wrappers=1 <file>
# format_name=mp3
# bit_rate=128000  (≈)
```
