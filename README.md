# Modrinth Downloader

A downloader for **Modrinth**, with features including search capabilities, automatic version matching, resumable downloads, and modpack extraction.
<details>
<summary><strong>Details</strong></summary>

- **Name**: Modrinth Downloader (modrinth_dl.py)
- **Version `[Major],[Minor],[Patch]`**: 1.1.1
- **Language**: Python 3.8+

</details>

## Requirements
- Python 3.8 or higher
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
Example - Search:
```bash
python modrinth_dl.py -s "fabric" -l fabric -v 1.20.1 --limit 20
```
Example - Download:
```bash
python modrinth_dl.py -c FTUde6eg -l fabric -v 1.20.1 -u
```
Example - Download specific projects:
```bash
python modrinth_dl.py -p "fabric-api,sodium,lithium" -l fabric -v 1.20.1
```

<details>
<summary><strong>Advanced Usage</strong></summary>

## Advanced Usage

### Search with Filters
```bash
# Search for mods with specific type
python modrinth_dl.py -s "optimization" -l fabric -v 1.20.1 -t mod

# Search with custom result limit
python modrinth_dl.py -s "shader" -l fabric -v 1.20.1 --limit 50
```

### Download Options
```bash
# Download to specific directory
python modrinth_dl.py -c "collection_id" -l fabric -v 1.20.1 -d "./my_collection"

# Download with verbose logging
python modrinth_dl.py -p "fabric-api" -l fabric -v 1.20.1 --verbose

# Download with custom worker count
python modrinth_dl.py -c collection_id -l fabric -v 1.20.1 -w 10

# Download with custom timeout and retries
python modrinth_dl.py -p "fabric-api" -l fabric -v 1.20.1 --timeout 60 -m 5

# Download only specific project types
python modrinth_dl.py -c collection_id -l fabric -v 1.20.1 -ot mod
```

### Network Options
```bash
# Custom User-Agent
python modrinth_dl.py -p "sodium" -l fabric -v 1.20.1 -ua "MyUserAgent/1.0"

# Ignore version checks for specific versions
python modrinth_dl.py -p "sodium" -l fabric -v 1.20.1 -i "version_id1,version_id2"
```

</details>

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

<details>
<summary><strong>To-Do List...</strong></summary>

**Currently the development is slow down, due personal reasons**

- Optimizing code **(Priority)**
- Add unit tests with pytest
- Create package distribution (PyPI) **(Probably no)**

</details>

## Notes
- Files are automatically organized by type (mods/, plugins/, resourcepacks/, shaderpacks/, datapacks/, etc.)
- Plugin loaders (Bukkit, Paper, Spigot, etc.) automatically use plugins/ folder
- Project mappings are persisted in `projectid.csv` for tracking (Updating)
- Comprehensive logs are stored in `download.log` with structured formatting
- All downloads support resume capability via HTTP Range requests
- Configuration is immutable and validated at creation time
- Network connectivity is verified before operations begin
- Exit Codes: `0`: Success, `1`: General error, `130`: Interrupted by user (Ctrl+C)

## License
This project is released under the Apache-2.0 license.
See the [LICENSE](./LICENSE) file for full terms.
