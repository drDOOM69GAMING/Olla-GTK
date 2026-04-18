# Olla-GTK

**A high-performance, native GTK3 workstation for Local and Cloud AI.**

Olla-GTK is a lightweight Linux desktop client built for users who demand deep control over their AI workflows. Originally built for Ollama, it has evolved into a multi-backend powerhouse that balances local inference with world-class cloud APIs (**Gemini** & **Claude**) within a single, streamlined interface.

## 🚀 The Native Advantage

Olla-GTK replaces bloated Electron wrappers with a **zero-overhead native experience**. By leveraging Python and the GTK3 toolkit, the application ensures system resources remain dedicated to your models—not your chat interface.

### Performance Architecture
The core of Olla-GTK is built on a strictly **non-blocking, event-driven architecture**. By utilizing Python’s `threading` library in conjunction with the **GLib main loop**, heavy I/O—such as streaming tokens or pulling multi-gigabyte model files—happens in the background. The UI remains 60FPS-smooth even during high-load inference.

## 🛠 Key Features

* **Multi-Backend Streaming:** Toggle instantly between **Ollama (Local)**, **Google Gemini**, and **Anthropic Claude**.
* **Unified Vision Integration:** Native support for multimodal reasoning (image-to-text) across all supported providers.
* **High-Inference Optimization:** Implements **100ms token-batching** logic, allowing the UI to handle extreme tokens-per-second (TPS) without lag.
* **Developer-First UX:**
    * **Smart Syntax Highlighting:** Built-in code block detection with high-contrast themes.
    * **Direct Code Extraction:** Right-click any code block to copy the contents instantly—no manual highlighting required.
    * **Background Model Management:** Download and verify Ollama models directly from the UI with live progress tracking.
* **✨ Edit Mode (Auto-Editor):** A specialized workflow that transforms the AI into a silent copyeditor. Paste raw text, and get back polished, corrected content without conversational filler.
* **Zero-Loss Recovery:** Automatically restores failed prompts to the input buffer if a network or inference error occurs.

## ⌨️ Keyboard Shortcuts

| Shortcut | Action |
| :--- | :--- |
| **Enter** | Send Message |
| **Shift + Enter** | Insert Newline (Literal) |
| **Ctrl + L** | Clear Chat & Reset History |
| **Ctrl + ,** | Open Settings & API Configuration |
| **Ctrl + Q** | Quit Application |

## 🧬 Technical Stack

* **Language:** Python 3
* **Toolkit:** GTK3 (PyGObject) + Cairo
* **API Strategy:** SSE-based streaming via `urllib`. 
    * *Zero heavy external dependencies (No `requests`, `aiohttp`, or `openai-python`).*
* **Concurrency:** Multi-threaded worker architecture with `GLib.idle_add` synchronization for thread-safe UI updates.
* **Security:** Local configuration is stored with `0o600` permissions; API keys are managed through a secure, persistent settings layer.

## 📦 Quick Start

### Prerequisites

```bash
# Ubuntu/Mint/Debian
sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0
```

### Installation

1.  **Clone & Run:**
    ```bash
    git clone https://github.com/drDOOM69GAMING/Olla-GTK.git
    cd Olla-GTK
    python3 Olla-GTK.py
    ```

2.  **Configure:** Press **Ctrl + ,** to enter your API keys for Gemini/Claude or to point the app toward your local Ollama instance.

---

## License

Copyright (c) 2026 drDOOM69GAMING  
Licensed under the **MIT License**. See [LICENSE](LICENSE) for details.

---
