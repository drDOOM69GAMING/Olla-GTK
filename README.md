# Olla-GTK

**A high-performance, native GTK3 workstation for Ollama.**

Olla-GTK is a lightweight Linux desktop client designed for users who run LLMs locally and demand deep control over their hardware. Built specifically to handle high-inference models on dedicated GPUs, it replaces heavy Electron wrappers with a streamlined, asynchronous Python core.

## Key Features

* **High-Performance Streaming:** Implements a 100ms token-batching logic. Even at high inference speeds, the UI remains responsive without locking the GTK main loop.
* **Real-Time Context Meter:** A visual indicator for VRAM and token usage. Tracks usage against your configured `num_ctx` limit to help prevent memory overflows.
* **Sliding-Window History:** Automatically manages conversation history to stay within hardware limits while preserving context coherence.
* **Developer-Focused UX:** * **Smart Syntax Highlighting:** Multi-language support for code blocks with a clean, high-contrast dark-mode aesthetic.
    * **Contextual Actions:** Dedicated right-click menu to instantly copy code blocks or text selections.
    * **Background Model Management:** Download and verify new models directly from the UI with live progress tracking.

## Technical Stack

* **Language:** Python 3
* **Toolkit:** GTK3 (PyGObject)
* **Backend:** Ollama API
* **Concurrency:** Multi-threaded architecture using GLib idle-handling for a completely non-blocking UI thread.

## Quick Start

### Prerequisites

1. **Ollama** must be installed and running:
   ```bash
   ollama serve
   ```
2. **Python GTK bindings** (Standard on most Linux distributions):
   ```bash
   # Ubuntu/Mint/Debian
   sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0
   ```

### Installation and Usage

Olla-GTK is a single-file application requiring no external Python package dependencies for core functionality.

1. Clone the repository:
   ```bash
   git clone https://github.com/your-username/Olla-GTK.git
   cd Olla-GTK
   ```
2. Run the script:
   ```bash
   python3 Olla-GTK.py
   ```

## Hardware Optimization

The settings panel allows for precise tuning of the model parameters based on your specific system resources:
* **Context Size (num_ctx):** Scale your memory window based on available VRAM.
* **GPU Layers (num_gpu):** Maximize offloading for faster tokens-per-second.
* **Predict Limit:** Set explicit output caps to prevent models from excessive generation and resource waste.

---

## License

MIT License
