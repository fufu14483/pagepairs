# 第三方组件与许可证声明

本文件由脚本对**实际分发的依赖**逐个读取 `dist-info` 元数据生成，不是人工罗列。
统计口径：便携包内置 Python 环境（`engine\python\Lib\site-packages`）中的全部
已安装发行版。

> **重要**：本项目的直接依赖 PyMuPDF、pdf2zh_next、babeldoc 均为 AGPL，
> 因此本项目整体以 AGPL-3.0 发布。此外 BabelDOC 硬依赖 **levenshtein（GPL-2.0-or-later）**，
> 该组件在便携包中被一并分发。详见下方「需要特别注意的组件」。


## 需要特别注意的组件

### 1. PyMuPDF — AGPL-3.0 或商业双许可

本项目核心引擎直接 `import pymupdf`。PyMuPDF 采用 AGPL-3.0 与 Artifex 商业许可双授权。以 AGPL-3.0 使用是免费的，但**分发二进制或通过网络提供服务时，必须按 AGPL-3.0 提供完整对应源码**。若需闭源分发，须自行向 Artifex 购买商业许可。

### 2. pdf2zh_next / BabelDOC — AGPL-3.0

在线翻译路径通过子进程调用 pdf2zh_next。两者均为 AGPL-3.0，与本项目许可一致。

### 3. levenshtein — GPL-2.0-or-later（易被忽略）

`babeldoc` 的 `METADATA` 中声明 `Requires-Dist: levenshtein>=0.27.1`，属**硬依赖**，非可选组件；BabelDOC 在 `format/pdf/document_il/midend/il_translator_llm_only.py` 顶层 `import Levenshtein` 用于计算编辑距离。

`levenshtein` 的许可证是 **GPL-2.0-or-later**。它在**便携包**中随附分发，在**源码仓库**中不包含（源码只做进程外调用，不复制、不链接其代码）。

GPL-2.0 与 AGPL-3.0 不自动兼容；但因该组件为 **or-later**，可选用 GPLv3，而 GPLv3 与 AGPLv3 兼容。**请务必在分发便携包时保留其许可证声明。**

### 4. 字体文件（便携包）

便携包 `engine\models\.cache\babeldoc\fonts\` 内含 34 个字体文件，**当前未随附任何字体许可证文件**。

涉及字体族：Noto Sans/Serif、Source Han Sans/Serif（思源）、LXGW WenKai（霞鹜文楷）、GoNotoKurrent、KleeOne、MaruBuri。

多数应为 SIL OFL 或 Apache-2.0，但**公开分发前必须逐个核对并补齐许可证全文**；OFL 另行要求：若对字体做了修改，不得继续使用原保留字体名（Reserved Font Name）。
本仓库不包含这些字体。


## 全部依赖清单

| 组件 | 许可证 |
|---|---|
| `aiofiles-24.1.0` | Apache-2.0 |
| `aiohappyeyeballs-2.7.1` | PSF-2.0 |
| `aiohttp-3.14.3` | Apache-2.0 AND MIT |
| `aiosignal-1.4.0` | Apache-2.0 |
| `annotated_doc-0.0.5` | MIT |
| `annotated_types-0.8.0` | MIT |
| `anyio-4.15.0` | MIT |
| `attrs-26.1.0` | MIT |
| `azure_ai_translation_text-1.0.1` | MIT |
| `azure_core-1.41.0` | MIT |
| `babeldoc-0.6.2` | AGPL-3.0 |
| `backports_zstd-1.7.0` | PSF-2.0 |
| `bitarray-3.11.0` | PSF-2.0 |
| `bitstring-4.4.0` | MIT |
| `blinker-1.9.0` | MIT |
| `certifi-2026.7.22` | MPL-2.0 |
| `cffi-2.1.1` | MIT |
| `chardet-7.6.0` | 0BSD |
| `charset_normalizer-3.5.1` | MIT |
| `click-8.5.0` | BSD-3-Clause |
| `cloudpickle-3.1.2` | BSD-3-Clause |
| `colorama-0.4.6` | BSD-3-Clause |
| `configargparse-1.7.5` | MIT |
| `cryptography-50.0.1` | Apache-2.0 AND BSD-3-Clause |
| `deepl-1.32.0` | MIT |
| `fastapi-0.141.1` | MIT |
| `ffmpy-1.0.0` | MIT |
| `filelock-3.32.5` | MIT |
| `flask-3.1.3` | BSD-3-Clause |
| `flatbuffers-25.12.19` | Apache-2.0 |
| `fonttools-4.64.0` | MIT |
| `freetype_py-2.5.1` | BSD-3-Clause |
| `frozenlist-1.8.0` | Apache-2.0 |
| `fsspec-2026.7.0` | BSD-3-Clause |
| `gradio-5.35.0` | Apache-2.0 |
| `gradio_client-1.10.4` | Apache-2.0 |
| `gradio_i18n-0.3.4` | Apache-2.0 |
| `gradio_pdf-0.0.22` | Apache-2.0 |
| `groovy-0.1.2` | MIT |
| `h11-0.16.0` | MIT |
| `hf_xet-1.6.0` | Apache-2.0 |
| `httpcore-1.0.9` | BSD-3-Clause |
| `httpcore2-2.12.0` | BSD-3-Clause |
| `httpx-0.28.1` | BSD-3-Clause |
| `httpx2-2.12.0` | BSD-3-Clause |
| `huggingface_hub-1.30.0` | Apache-2.0 |
| `hyperscan-0.8.2` | MIT |
| `idna-3.19` | BSD-3-Clause |
| `imageio-2.37.4` | BSD-2-Clause |
| `isodate-0.7.2` | BSD-3-Clause |
| `itsdangerous-2.2.0` | BSD-3-Clause |
| `jinja2-3.1.6` | BSD-3-Clause |
| `jiter-0.16.0` | MIT |
| `joblib-1.6.0` | BSD-3-Clause |
| `langcodes-3.4.1` | MIT |
| `language_data-1.4.0` | MIT |
| `lazy_loader-0.5` | BSD-3-Clause |
| `levenshtein-0.27.4` | GPL-2.0-or-later |
| `lxml-6.1.3` | BSD-3-Clause |
| `marisa_trie-1.4.1` | LGPL-2.1-or-later AND MIT AND BSD-2-Clause |
| `markdown_it_py-4.2.0` | MIT |
| `markupsafe-3.0.3` | BSD-3-Clause |
| `mdurl-0.1.2` | MIT |
| `ml_dtypes-0.6.0` | Apache-2.0 |
| `msgpack-1.2.2` | Apache-2.0 |
| `multidict-6.7.1` | Apache-2.0 |
| `narwhals-2.25.0` | MIT |
| `networkx-3.6.1` | BSD-3-Clause |
| `numpy-2.5.2` | MIT AND BSD-3-Clause AND 0BSD AND Zlib AND CC0-1.0 |
| `ollama-0.6.2` | MIT |
| `onnx-1.22.0` | Apache-2.0 |
| `onnxruntime-1.29.0` | MIT |
| `openai-3.7.0` | Apache-2.0 |
| `opencv_python_headless-5.0.0.93` | Apache-2.0 |
| `orjson-3.12.0` | Apache-2.0 AND MIT AND MPL-2.0 |
| `packaging-26.3` | Apache-2.0 AND BSD-2-Clause |
| `pandas-2.3.3` | BSD-3-Clause |
| `pdf2zh_next-2.9.0` | AGPL-3.0 |
| `peewee-4.4.0` | MIT |
| `pillow-11.3.0` | MIT-CMU AND MIT |
| `propcache-0.5.2` | Apache-2.0 |
| `protobuf-7.36.1` | BSD-3-Clause |
| `psutil-7.2.2` | BSD-3-Clause |
| `pycparser-3.0` | BSD-3-Clause |
| `pydantic-2.11.10` | MIT |
| `pydantic_core-2.33.2` | MIT |
| `pydantic_settings-2.15.0` | MIT |
| `pydub-0.25.1` | MIT |
| `pygments-2.21.0` | BSD-2-Clause |
| `pymupdf-1.28.2` | AGPL-3.0 |
| `pypdf-6.16.2` | BSD-3-Clause |
| `python_dateutil-2.9.0.post0` | Apache-2.0 |
| `python_dotenv-1.2.3` | BSD-3-Clause |
| `python_multipart-0.0.32` | Apache-2.0 |
| `pytz-2026.3.post1` | MIT |
| `pyyaml-6.0.3` | MIT |
| `pyzstd-0.19.1` | BSD-3-Clause |
| `rapidfuzz-3.14.6` | MIT |
| `regex-2026.9.3` | Apache-2.0 AND CNRI-Python |
| `requests-2.34.2` | Apache-2.0 |
| `rich-15.0.0` | MIT |
| `rtree-1.4.1` | MIT |
| `ruff-0.16.5` | MIT |
| `safehttpx-0.1.7` | MIT |
| `scikit_image-0.26.0` | BSD-3-Clause |
| `scikit_learn-1.9.0` | BSD-3-Clause |
| `scipy-1.18.1` | BSD-3-Clause |
| `semantic_version-2.10.0` | BSD-3-Clause |
| `shellingham-1.5.4` | ISC |
| `six-1.17.0` | MIT |
| `sniffio-1.3.1` | Apache-2.0 AND MIT |
| `socksio-1.0.0` | MIT |
| `sse_starlette-3.4.10` | BSD-3-Clause |
| `starlette-0.52.1` | BSD-3-Clause |
| `tenacity-9.1.4` | Apache-2.0 |
| `tencentcloud_sdk_python_common-3.1.166` | Apache-2.0 |
| `tencentcloud_sdk_python_tmt-3.1.129` | Apache-2.0 |
| `threadpoolctl-3.6.0` | BSD-3-Clause |
| `tibs-0.5.7` | MIT |
| `tifffile-2026.8.23` | BSD-3-Clause |
| `tiktoken-0.14.0` | MIT |
| `toml-0.10.2` | MIT |
| `tomlkit-0.13.3` | MIT |
| `toposort-1.10` | Apache-2.0 |
| `tqdm-4.70.0` | MIT AND MPL-2.0 |
| `truststore-0.10.4` | MIT |
| `typer-0.27.2` | MIT |
| `typing_extensions-4.16.0` | PSF-2.0 |
| `typing_inspection-0.4.4` | MIT |
| `tzdata-2026.3` | Apache-2.0 |
| `uharfbuzz-0.56.1` | Apache-2.0 |
| `urllib3-2.7.0` | MIT |
| `uvicorn-0.52.4` | BSD-3-Clause |
| `websockets-15.0.1` | BSD-3-Clause |
| `werkzeug-3.1.8` | BSD-3-Clause |
| `xinference_client-3.2.0` | Apache-2.0 |
| `xsdata-26.2` | MIT |
| `yarl-1.24.5` | Apache-2.0 |

## 许可证汇总

| 许可证 | 组件数 |
|---|---|
| MIT | 50 |
| BSD-3-Clause | 37 |
| Apache-2.0 | 28 |
| PSF-2.0 | 4 |
| AGPL-3.0 ⚠️ | 3 |
| Apache-2.0 AND MIT | 2 |
| BSD-2-Clause | 2 |
| 0BSD | 1 |
| Apache-2.0 AND BSD-2-Clause | 1 |
| Apache-2.0 AND BSD-3-Clause | 1 |
| Apache-2.0 AND CNRI-Python | 1 |
| Apache-2.0 AND MIT AND MPL-2.0 | 1 |
| GPL-2.0-or-later ⚠️ | 1 |
| ISC | 1 |
| LGPL-2.1-or-later AND MIT AND BSD-2-Clause | 1 |
| MIT AND BSD-3-Clause AND 0BSD AND Zlib AND CC0-1.0 | 1 |
| MIT AND MPL-2.0 | 1 |
| MIT-CMU AND MIT | 1 |
| MPL-2.0 | 1 |

---

生成方式：读取每个 `*.dist-info/METADATA` 的 `License-Expression`（PEP 639，最权威）→ `License` 字段 → `Classifier: License ::` → 包内 LICENSE 文件正文，逐级回退。总计 138 个发行版，无未归类项。
