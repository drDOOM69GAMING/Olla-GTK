<img src="https://github.com/user-attachments/assets/1c484f70-4da1-418d-b629-03ddaf5b614f" alt="Olla-GTK Screenshot" width="100%" />

# Olla-GTK

**A high-performance, native GTK3 workstation for Ollama.**

Olla-GTK is a lightweight Linux desktop client designed for users who run LLMs locally and demand deep control over their hardware. Built specifically to handle high-inference models on dedicated GPUs, it replaces heavy Electron wrappers with a streamlined, asynchronous Python core.

## Extended Description

Olla-GTK was born out of a need for a minimalist yet powerful interface for local large language models. While many existing tools rely on resource-heavy web technologies, Olla-GTK leverages the native performance of the GTK3 toolkit to provide a near-zero overhead experience. This ensures that your system resources remain dedicated to what matters most: the inference engine.

### Performance Architecture
The core of Olla-GTK is built on a strictly non-blocking, event-driven architecture. By utilizing Python's threading library in conjunction with the GLib main loop, the application performs heavy I/O operations—such as streaming large model responses or pulling multi-gigabyte model files—in the background. This prevents the user interface from stuttering or becoming unresponsive during high-load scenarios.

### Advanced Memory and Context Management
Managing VRAM is the primary challenge of local inference. Olla-GTK provides a transparent view into the model's context window.

* **The Context Meter:** This feature calculates the approximate token usage in real-time, allowing users to visualize how close they are to the hardware limits of their GPU.
* **Sliding-Window Logic:** To maintain long-running conversations without crashing the backend, the application implements an automated history truncation system. It intelligently removes the oldest turns in the conversation to ensure the total token count stays within the user-defined `num_ctx` limit.

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
   git clone [https://github.com/drDOOM69GAMING/Olla-GTK.git](https://github.com/drDOOM69GAMING/Olla-GTK.git)
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

Copyright (c) 2026 drDOOM69GAMING

This project is licensed under the **MIT License**. For the full license text, please see the [LICENSE](LICENSE) file in the repository root.
```
