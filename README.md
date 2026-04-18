<img width="2034" height="1612" alt="Screenshot from 2026-04-18 10-32-11" src="https://github.com/user-attachments/assets/7175ac3b-a7f7-405f-b386-fa2756632a78" />

# Olla-GTK

**A high-performance, native GTK3 workstation for Local and Cloud AI.**

Olla-GTK is a lightweight Linux desktop client designed for users who demand deep control over their AI workflows. Originally built for Ollama, it has evolved into a multi-backend workstation that handles high-inference local models alongside world-class cloud APIs (**Gemini** & **Claude**) within a single, streamlined interface.

## Extended Description

Olla-GTK replaces heavy Electron wrappers with a near-zero overhead experience. By leveraging the native performance of the GTK3 toolkit, the application ensures that your system resources remain dedicated to the inference engine—whether that’s a local GPU or high-speed cloud streaming.

### Performance Architecture
The core of Olla-GTK is built on a strictly non-blocking, event-driven architecture. By utilizing Python's threading library in conjunction with the GLib main loop, the application performs heavy networking and I/O—such as streaming model responses or pulling multi-gigabyte model files—in the background. This prevents the UI from stuttering even during high-load scenarios.

### Multi-Backend & Vision Integration
* **Unified Vision Support:** Native support for image-to-text (Multimodal) across Ollama, Gemini 1.5, and Claude 3.
* **The Context Meter:** Calculates approximate token usage in real-time, allowing you to visualize usage against hardware or API limits.
* **Sliding-Window Logic:** Implements automated history truncation to maintain long-running conversations without exceeding `num_ctx` or API caps.
* **✨ Edit Mode:** A specialized workflow that hijacks the system prompt to silently copy-edit pasted text, returning only the polished version without conversational filler.

## Key Features

* **Multi-Backend Streaming:** Seamlessly switch between **Ollama (Local)**, **Google Gemini**, and **Anthropic Claude**.
* **Multimodal Reasoning:** Attach images for visual analysis across all supported providers.
* **High-Performance Streaming:** Implements 100ms token-batching logic to handle high tokens-per-second (TPS) without locking the main thread.
* **Developer-Focused UX:**
    * **Smart Syntax Highlighting:** Multi-language support for code blocks with high-contrast themes.
    * **Direct Code Extraction:** Right-click any code block to copy its contents instantly—no manual highlighting required.
    * **Background Model Management:** Download and verify local Ollama models directly from the UI with live progress tracking.
* **Zero-Loss Recovery:** Automatically restores your prompt to the input buffer if a network or inference error occurs, preventing lost work.

## Keyboard Shortcuts

| Shortcut | Action |
| :--- | :--- |
| **Enter** | Send Message |
| **Shift + Enter** | Insert Newline |
| **Ctrl + L** | Clear Chat & History |
| **Ctrl + ,** | Open Settings |
| **Ctrl + Q** | Quit |

## Technical Stack

* **Language:** Python 3
* **Toolkit:** GTK3 (PyGObject)
* **API Integration:** SSE-based streaming via `urllib` (Standard library focus; zero heavy external dependencies like `requests`).
* **Concurrency:** Multi-threaded architecture using GLib idle-handling for a completely non-blocking UI.

## Quick Start

### Prerequisites

1. **Ollama** (Optional, for local inference):
   ```bash
   ollama serve
   ```
2. **Python GTK bindings**:
   ```bash
   # Ubuntu/Mint/Debian
   sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0
   ```

### Installation and Usage

1. Clone the repository:
   ```bash
   git clone https://github.com/drDOOM69GAMING/Olla-GTK.git
   cd Olla-GTK
   ```
2. Run the workstation:
   ```bash
   python3 Olla-GTK.py
   ```

## Hardware & API Optimization

The settings panel (**Ctrl+,**) allows for precise tuning:
* **Context Size (num_ctx):** Scale your memory window based on available VRAM.
* **GPU Layers (num_gpu):** Maximize offloading for faster local TPS.
* **Cloud Integration:** Securely configure API keys and model selections for Gemini and Claude.

---

## License

Copyright (c) 2026 drDOOM69GAMING

This project is licensed under the **MIT License**. For the full license text, please see the [LICENSE](LICENSE) file in the repository root.

---
