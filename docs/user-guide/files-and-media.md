# files and media

What users can attach to a chat and how RECALL turns it into something a model can read. Defaults live in the `video`, `pdf`
and `speech_to_text` blocks of [settings.yaml](../reference/settings-yaml.md). Limits: 10 MB per file, 25 MB per web message,
22 MB per Telegram file. Images are sent as they are to models that declare image input; audio is always transcribed first.

## video input

A video sent to a chat model that declares `image` but not `video` input is
decomposed before the model runs. RECALL uses `ffmpeg` and `ffprobe` from the
`PATH` to sample one frame every `video.frame_interval_seconds` (up to
`video.max_frames`, scaled to `video.max_width` pixels), and sends the frames as
image attachments in order. The audio track, when present, is transcribed with
the speech-to-text flow below. The message carries a note with the frame
timestamps, whether only part of the video was sampled, and whether the video
has an audio track, so the model knows the frames come from a decomposed video.
Models with native `video` input receive the file unchanged. Models without
`image` input still reject videos. Sampling stops after the last frame, and the
audio extraction skips video decoding, to keep CPU use low.

When this happens the web UI and Telegram show a warning above the reply, because
the model sees only the sampled frames and may miss motion or events between them.

The defaults live in `settings.yaml` under `video`. Install `ffmpeg` (which
includes `ffprobe`) on the host running RECALL; a missing binary produces an
explicit error for video messages only.

## pdf input

The web composer and Telegram accept PDFs. RECALL reads them locally before the model runs: PyMuPDF
extracts the selectable text of each page, and a page with fewer than `pdf.ocr_min_chars` non-whitespace
characters (default 25) is rendered at `pdf.ocr_dpi` and read with Tesseract as a whole. A page with enough
text is still read with OCR for each large embedded image (at least 5% of the page area, up to 10 images per
page), because scanned or photographed details such as a card's name and address are often stored only as
images. Small images such as logos are skipped, and pages without large images skip OCR. The OCR text of an image
is added after the page's digital text, so a heading that appears in both may show up twice. The model receives
only text: no PDF file and no page images. Vector graphics and charts are not interpreted, except text Tesseract
recognizes inside images.

The text is added to the user's message under `Attached document: <name>`, with a `[Page N]` marker before each
page (`[Page N, OCR]` for OCR pages) so answers can cite pages. The stored turn keeps this text, not the file,
so later chats can recall it like any other message.

Limits: `pdf.max_pages` (default 30) per PDF; longer documents are rejected. Files are subject to the channel
size limits (10 MB per file and 25 MB per web message, 22 MB per Telegram file). Password-protected and invalid
files are rejected with an explicit error. Tesseract must be installed as a system program (see
[Tesseract OCR](#tesseract-ocr)); without it, PDFs with selectable text still work, and a scanned
page fails with an error that names the fix. `pdf.ocr_languages` must match the installed language packs.

The defaults live in `settings.yaml` under `pdf`.

## tesseract ocr

Scanned PDF pages are read with the Tesseract OCR program. The Python package `pytesseract` only wraps
the program, and `uv sync` does not install it, so install it on the machine that runs RECALL (the web UI
and the Telegram bot). PDFs with selectable text do not need it.

Windows (PowerShell or Command Prompt):

```
winget install --id UB-Mannheim.TesseractOCR -e
```

The installer is also available from the [UB-Mannheim Tesseract page](https://github.com/UB-Mannheim/tesseract/wiki).
In its component list, select the Spanish language data if you want Spanish OCR.

macOS (Homebrew):

```
brew install tesseract tesseract-lang
```

Debian or Ubuntu:

```
sudo apt install tesseract-ocr tesseract-ocr-eng tesseract-ocr-spa
```

Fedora:

```
sudo dnf install tesseract tesseract-langpack-eng tesseract-langpack-spa
```

Check the installation in a new terminal:

```
tesseract --version
tesseract --list-langs
```

The language list must include every code in `pdf.ocr_languages` (default `eng+spa`). To use other
languages, install their packs and change `pdf.ocr_languages` in `settings.yaml`.

RECALL finds the executable in this order: `pdf.tesseract_cmd` in `settings.yaml` (full path), then the
`PATH`, then `C:\Program Files\Tesseract-OCR\tesseract.exe` on Windows. Restart the web UI and the Telegram
bot after installing, so they see the new `PATH`. If Tesseract is missing, a scanned page fails with an
error that names this section; PDFs with selectable text still work.

### windows notes

- `winget` may install Tesseract per user, in `%LOCALAPPDATA%\Programs\Tesseract-OCR`, instead of
  `C:\Program Files\Tesseract-OCR`. RECALL does not search that folder, so add it to the user `PATH`
  (PowerShell; open a new terminal afterwards):

  ```
  [Environment]::SetEnvironmentVariable('Path', [Environment]::GetEnvironmentVariable('Path','User') + ';' + "$env:LOCALAPPDATA\Programs\Tesseract-OCR", 'User')
  ```

  Check whether it is already there first: `[Environment]::GetEnvironmentVariable('Path','User') -split ';'`.
- A terminal or process started before the `PATH` change does not see it. Open a new terminal, or reload
  the variable in the current one with `$env:Path = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User')`.
  Then restart the web UI and the Telegram bot from that terminal.
- The installer's Spanish data may be missing. Download it into the `tessdata` folder of the installation,
  then check with `tesseract --list-langs`:

  ```
  Invoke-WebRequest -Uri 'https://github.com/tesseract-ocr/tessdata/raw/main/spa.traineddata' -OutFile "$env:LOCALAPPDATA\Programs\Tesseract-OCR\tessdata\spa.traineddata"
  ```

  Use the same folder for other languages, for example `eng.traineddata`. Pack files from other sources must
  match the Tesseract version; use the `tessdata` repository above.

## message tags and timestamps

Each user turn is sent to the model prefixed with its local timestamp and the channel
it was written from (for example `[2026-10-31 14:01+01:00 via telegram]` or
`[2026-10-31 14:01+01:00 via web UI]`), taken from the `ts` and `channel` stored with
the turn, so reloaded chats keep the same dates and channels. Turns stored before the
channel was recorded, and turns written by the system, carry only the timestamp. Assistant replies are not prefixed. Text derived from
audio starts with `[AUDIO]`, and text describing a video starts with `[VIDEO]`.
Stored conversations keep the original message text; the prefixes are added only
to the context sent to the model.

## voice messages in the web UI

The microphone button in the web composer records from the browser's microphone. Press it to start
(it turns red and shows the elapsed time), press it again to stop: the recording is attached as an
audio file and sent like any other audio, so it is transcribed first (see below) and the model
receives the `[AUDIO]` text, the same as a Telegram voice note. Browsers allow the microphone only
on HTTPS or `localhost`; where it is unavailable the button stays disabled.

## attachments in the web UI

- **Paste:** Ctrl+V in the composer attaches a copied image or file (PDF, audio, video) like the
  paperclip, with the same type and size limits (10 MB per file, 25 MB in total). Pasted
  screenshots are named `pasted-<date>-<time>.png`. If the clipboard also holds text, only the
  text is pasted.
- **Preview:** images show as thumbnails, audio with a player and video with controls inside the
  message. The original audio and video are stored in the turn (`media`) and survive a reload;
  they take disk space in the chat file and are not indexed for memory search.
- **Transcripts:** each voice note keeps its transcript (`media[].transcript`), shown under the
  player after a reload too. If the transcription comes back empty the message is not sent and
  the audio is not stored; the error says no speech was detected. Each transcription is logged
  with file name, format, MIME type, size, model, language and characters returned.

## files and notices from the agent

An agent can hand you a file and message you on Telegram, with two tool groups you pick per agent (see [tools](../reference/tools.md#files_send)):

- `files_send` (level `reader`): `send_file` sends a file the agent can already read, for example a note from your Obsidian folder. It shows in the web chat (images,
  audio and video are previewed, other files are a download link), is kept with the reply so it is still there after a reload, and, in a Telegram chat, arrives as a
  photo, audio, video or document. Up to 10 MB a file and 25 MB a reply. It opens no new access: a file on a terminal machine (`<machine>:<path>`) is fetched through the terminal and asks for the same approval card as reading it there,
  and if you are not there to answer it the send fails after the card's timeout and the agent is told.
- `notify` (level `assistant`): `notify_telegram` sends you a short message on Telegram, for example "tell me when you finish". It needs the bot token on the server and
  your user linked in `TELEGRAM_ALLOWED_USERS`; at most 30 an hour per agent.

## pdf files made by the agent

With the `memory_edit` group an agent has `make_pdf`: it describes the document (title, then headings, paragraphs, bullet and numbered lists, tables, images from a source it can read, spacers and page breaks; `**bold**` and `*italic*` work in the text) and RECALL lays it out and saves the PDF.

- **Where it goes:** the agent can name the place as `<source>:<path>.pdf` in any source it may write (its mind palace, or a folder that is not read-only). Without a path it saves to `mind_palace:documents/<title>.pdf`. A name that is taken gets a number (`-2`, `-3`) unless the agent asks to overwrite. The agent can move the file afterwards with `move` or `folder_move`.
- **Sending it to you:** `make_pdf` only saves the file; to receive it the agent also needs `files_send` (`send_file`).
- **Reading it back:** `read` and `folder_read` return a PDF as text with a `[Page N]` marker per page (digital text, no OCR), and `view_image` lets a model that accepts images *look* at pages (one page, or up to 6 as a contact sheet), which is how an agent checks the layout of what it made. The picture is shrunk, shown to the model once and removed from its context at your next message; at most 6 per message. The index does not read PDFs.
- **Limits:** standard fonts only, so accents and the usual Western punctuation work but arrows, check marks and similar symbols cannot be drawn: the agent gets a warning naming them. At most 500 elements, 2000 table cells, 200,000 characters of text and 10 MB per file.

## charts and diagrams made by the agent

With the opt-in `charts` group an agent has `render_chart` (Vega-Lite charts) and `render_diagram` (Graphviz) and saves PNG images like `make_pdf` saves PDFs (default `mind_palace:charts/`). It can look at the result with `view_image`; to receive it you also need `files_send`. Diagrams need Graphviz on the machine (included in the Docker image; otherwise `apt install graphviz`). Details: [tools](../reference/tools.md#charts).

## speech-to-text

Audio is always transcribed before the chat model is invoked. RECALL uses
OpenRouter's `/api/v1/audio/transcriptions` endpoint with the configured model
and routing policy, then stores the transcription as text in the conversation.
This keeps later model changes independent of whether the selected chat model
supports audio input.

The transcription model is a single user setting, the **Default STT model** in
web **Settings** or Telegram `/settings`; it is shared by all chats and agents.
The initial default is `openai/whisper-large-v3-turbo`.
The spoken language is `models.stt_language` in `user.yaml` (a code such as `es` or `es-ES`; set it with
`python -m src.admin user set --user U --set models.stt_language=es`, no restart). Precedence: the user's
`models.stt_language`, then `speech_to_text.language` in `settings.yaml` (instance default), then the
provider's auto-detection, which can misread short clips. When ZDR applies
(`speech_to_text.zdr`, else the user's `require_zdr`) the model must have an
active ZDR transcription endpoint and requests carry `provider: {"zdr": true}`;
otherwise audio requests fail explicitly. Without ZDR the plain public
transcription catalog is used and no `provider` field is sent.
OpenRouter's current transcription catalog can be queried through
`GET /api/speech-to-text/models?q=<query>&zdr=true`; without `zdr` the endpoint follows
that policy. Telegram `/stt` shows the
current default.
