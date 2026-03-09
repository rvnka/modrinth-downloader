# Modrinth Downloader

A downloader for **Modrinth**, with features including search capabilities, automatic version matching, resumable downloads, and modpack extraction.

> **Currently the development is slowed down, due personal reasons**

## Requirements
- Python 3.10 or higher
- Standard library only (no external dependencies)
- Internet connection for API access
- Sufficient disk space for downloads

## Installation
**Use curl**
```bash
curl -L -o modrinth_dl.py https://raw.githubusercontent.com/rvnka/modrinth-downloader/refs/heads/main/modrinth_dl.py
```
or **Clone this repository**
```bash
git clone https://github.com/rvnka/modrinth-downloader.git && cd modrinth-downloader
```

## Usage
Display full help:
```bash
python modrinth_dl.py -h
```

## Features
- **Search functionality** - Search for projects directly from CLI with filters
- **Download from collections** - Download all projects from a Modrinth collection
- **Smart version matching** - Automatically select compatible versions based on MC version and loader
- **Resume + retry** - Robust download with automatic resume and exponential backoff retry
- **Update mode** (-u) - Intelligently replace outdated files with latest compatible versions
- **Modpack support** - Extract .mrpack files and automatically download their dependencies
- **Parallel processing** - Configurable multi-threading for faster downloads
- **Plugin loader support** - Automatically detect and organize plugin loader projects
- **Direct Minecraft integration** - Automatically organize files by type
- **Fully configurable** - Extensive command-line flags for customization
- **And many more...**

## License
This project is released under the Apache-2.0 license.
See the [LICENSE](./LICENSE) file for full terms.
