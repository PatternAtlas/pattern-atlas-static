# Copyright 2024 University of Stuttgart
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import re
from hashlib import sha3_256
from html.parser import HTMLParser
from pathlib import Path
from json import dumps, loads

from httpx import get
from jinja2 import Environment, PackageLoader, select_autoescape
from markupsafe import Markup
from mistune import create_markdown
from mistune.plugins import math
from mistune.util import escape

from .model import AtlasContent, Pattern, PatternLanguage

# monkeypatch math plugin to use better regexes
# https://github.com/lepture/mistune/issues/330
math.BLOCK_MATH_PATTERN = r"(?!^ {4,})\$\$\s*(?P<math_text>\S.*?)\s*(?<!\\)\$\$"
math.INLINE_MATH_PATTERN = r"\$\s*(?P<math_text>\S.*?)\s*(?<!\\)\$"

# monkeyfix mistune to correctly escape rendered math
_render_block_math = math.render_block_math
_render_inline_math = math.render_inline_math


def render_block_math_fixed(renderer, text):
    return _render_block_math(renderer, escape(text))


def render_inline_math_fixed(renderer, text):
    return _render_inline_math(renderer, escape(text))


math.render_block_math = render_block_math_fixed
math.render_inline_math = render_inline_math_fixed


_CAMEL_CASE_REGEX = re.compile(r"([a-zäöü])([A-ZÄÖÜ])")


def _camel_case_replacer(match: re.Match) -> str:
    return f"{match[1]} {match[2]}".lower()


def split_camel_case(text: str) -> str:
    return _CAMEL_CASE_REGEX.sub(_camel_case_replacer, text)


class ExtractImageLinksParser(HTMLParser):
    def __init__(self, *, convert_charrefs: bool = True) -> None:
        super().__init__(convert_charrefs=convert_charrefs)
        self.image_links = set()

    def reset(self) -> None:
        self.image_links = set()
        return super().reset()

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "img":
            for attr, value in attrs:
                if attr == "src":
                    self.image_links.add(value)


class StaticRender:
    def __init__(self, location: Path, is_planqk: bool = False) -> None:
        if not location.is_dir() or not location.exists():
            raise ValueError
        self.location = location
        self.is_planqk = is_planqk
        self._resource_map = {"": "/assets/empty.svg"}
        self._jinja = Environment(
            loader=PackageLoader("static_patternatlas"),
            autoescape=select_autoescape(
                enabled_extensions=("html", "jinja2", "css"), default_for_string=True
            ),
        )
        self._jinja.filters["resource"] = self._resource
        self._jinja.filters["markdown"] = self._markdown
        self._jinja.filters["split_camel_case"] = split_camel_case
        self._mistune = create_markdown(
            escape=True, plugins=["table", "footnotes", "math", "url", "task_lists"]
        )
        self._load_asset_map()

    def _load_asset_map(self):
        asset_folder = self.location / "assets"
        path = asset_folder / "asset_map.json"
        if path.exists():
            try:
                resources = loads(path.read_text())
                for key, value in resources.items():
                    assert (
                        isinstance(key, str)
                        and isinstance(value, str)
                        and value.startswith("/assets/")
                    )
                    asset_path = self.location / value[1:]
                    assert asset_path.relative_to(asset_folder)
                    if asset_path.exists():
                        self._resource_map[key] = value
            except Exception:
                print("Could not load asset map!")

    def _save_asset_map(self):
        asset_folder = self.location / "assets"
        asset_folder.mkdir(parents=True, exist_ok=True)
        path = asset_folder / "asset_map.json"
        path.write_text(dumps(self._resource_map))

    def _resource(self, url: str) -> str:
        if url is None:
            url = ""

        resolved = self._resource_map.get(url)
        if resolved:
            return resolved

        try:
            response = get(
                url, headers={"Accept": "*/*"}, timeout=3, follow_redirects=True
            )
            response.raise_for_status()
            byte_content = response.content
            suffixes = Path(response.url.path).suffixes
            asset_url = f"/assets/{sha3_256(byte_content).hexdigest()}{''.join(suffixes)}"
            path = self.location / asset_url[1:]
            if not path.exists():
                path.write_bytes(byte_content)
            self._resource_map[url] = asset_url
        except Exception:
            print(f"Failed to download resource: {url}")
            self._resource_map[url] = "/assets/empty.svg#" + url

        return self._resource_map[url]

    def _markdown(self, markdown: str):
        if not markdown:
            return "–"
        html = self._mistune(markdown.strip())
        assert isinstance(html, str)
        html = html.strip()
        if html.startswith("<p>") and html.endswith("</p>"):
            html = html[3:-4]

        image_link_parser = ExtractImageLinksParser()
        image_link_parser.feed(html)
        for link in image_link_parser.image_links:
            replacement = self._resource(link)
            html = html.replace(link, replacement)

        html = html.replace('href="pattern-languages/', 'href="/pattern-languages/')
        html = html.replace('href="http', 'target="_blank" href="http')
        return Markup(html)

    def render_all(self, atlas: AtlasContent):
        self.render_empty_picture_asset()
        self.download_katex_fonts()
        self.render_styles()
        self.render_index(atlas)
        for lang in atlas.languages.values():
            self.render_language_overview(atlas, lang)
            for pattern_id in lang.patterns:
                pattern = atlas.patterns[pattern_id]
                self.render_pattern(atlas, pattern, lang)
        self._save_asset_map()

    def render_empty_picture_asset(self):
        folder_path = self.location / "assets"
        folder_path.mkdir(parents=True, exist_ok=True)
        path = folder_path / "empty.svg"
        template = self._jinja.get_template("empty.svg")
        svg = template.render()
        path.write_text(svg)

    def download_katex_fonts(self):
        folder_path = self.location / "assets" / "fonts"
        folder_path.mkdir(parents=True, exist_ok=True)
        for font in (
            "KaTeX_AMS-Regular.ttf",
            "KaTeX_AMS-Regular.woff",
            "KaTeX_AMS-Regular.woff2",
            "KaTeX_Caligraphic-Bold.ttf",
            "KaTeX_Caligraphic-Bold.woff",
            "KaTeX_Caligraphic-Bold.woff2",
            "KaTeX_Caligraphic-Regular.ttf",
            "KaTeX_Caligraphic-Regular.woff",
            "KaTeX_Caligraphic-Regular.woff2",
            "KaTeX_Fraktur-Bold.ttf",
            "KaTeX_Fraktur-Bold.woff",
            "KaTeX_Fraktur-Bold.woff2",
            "KaTeX_Fraktur-Regular.ttf",
            "KaTeX_Fraktur-Regular.woff",
            "KaTeX_Fraktur-Regular.woff2",
            "KaTeX_Main-Bold.ttf",
            "KaTeX_Main-Bold.woff",
            "KaTeX_Main-Bold.woff2",
            "KaTeX_Main-BoldItalic.ttf",
            "KaTeX_Main-BoldItalic.woff",
            "KaTeX_Main-BoldItalic.woff2",
            "KaTeX_Main-Italic.ttf",
            "KaTeX_Main-Italic.woff",
            "KaTeX_Main-Italic.woff2",
            "KaTeX_Main-Regular.ttf",
            "KaTeX_Main-Regular.woff",
            "KaTeX_Main-Regular.woff2",
            "KaTeX_Math-BoldItalic.ttf",
            "KaTeX_Math-BoldItalic.woff",
            "KaTeX_Math-BoldItalic.woff2",
            "KaTeX_Math-Italic.ttf",
            "KaTeX_Math-Italic.woff",
            "KaTeX_Math-Italic.woff2",
            "KaTeX_SansSerif-Bold.ttf",
            "KaTeX_SansSerif-Bold.woff",
            "KaTeX_SansSerif-Bold.woff2",
            "KaTeX_SansSerif-Italic.ttf",
            "KaTeX_SansSerif-Italic.woff",
            "KaTeX_SansSerif-Italic.woff2",
            "KaTeX_SansSerif-Regular.ttf",
            "KaTeX_SansSerif-Regular.woff",
            "KaTeX_SansSerif-Regular.woff2",
            "KaTeX_Script-Regular.ttf",
            "KaTeX_Script-Regular.woff",
            "KaTeX_Script-Regular.woff2",
            "KaTeX_Size1-Regular.ttf",
            "KaTeX_Size1-Regular.woff",
            "KaTeX_Size1-Regular.woff2",
            "KaTeX_Size2-Regular.ttf",
            "KaTeX_Size2-Regular.woff",
            "KaTeX_Size2-Regular.woff2",
            "KaTeX_Size3-Regular.ttf",
            "KaTeX_Size3-Regular.woff",
            "KaTeX_Size3-Regular.woff2",
            "KaTeX_Size4-Regular.ttf",
            "KaTeX_Size4-Regular.woff",
            "KaTeX_Size4-Regular.woff2",
            "KaTeX_Typewriter-Regular.ttf",
            "KaTeX_Typewriter-Regular.woff",
            "KaTeX_Typewriter-Regular.woff2",
        ):
            path = folder_path / font
            if path.exists():
                continue
            try:
                response = get(
                    f"https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/fonts/{font}",
                    headers={"Accept": "*/*"},
                    timeout=3,
                )
                response.raise_for_status()
                path.write_bytes(response.content)
            except Exception:
                print(f"Failed to download font: {font}")

    def render_styles(self):
        path = self.location / "styles.css"
        template = self._jinja.get_template("styles.css")
        css = template.render()
        path.write_text(css)

    def render_index(self, atlas: AtlasContent):
        path = self.location / "index.html"
        template = self._jinja.get_template("languages.jinja2")
        html = template.render(atlas=atlas, is_planqk=self.is_planqk)
        path.write_text(html)
        folder_path = self.location / "pattern-languages"
        folder_path.mkdir(parents=True, exist_ok=True)
        path = folder_path / "index.html"
        path.write_text(html)

    def render_language_overview(self, atlas: AtlasContent, language: PatternLanguage):
        folder_path = self.location / "pattern-languages" / language.language_id
        folder_path.mkdir(parents=True, exist_ok=True)
        path = folder_path / "index.html"
        template = self._jinja.get_template("language-overview.jinja2")
        html = template.render(atlas=atlas, language=language, is_planqk=self.is_planqk)
        path.write_text(html)

    def render_pattern(
        self, atlas: AtlasContent, pattern: Pattern, language: PatternLanguage
    ):
        folder_path = (
            self.location
            / "pattern-languages"
            / language.language_id
            / pattern.pattern_id
        )
        folder_path.mkdir(parents=True, exist_ok=True)
        path = folder_path / "index.html"
        template = self._jinja.get_template("pattern.jinja2")
        html = template.render(
            atlas=atlas, pattern=pattern, language=language, is_planqk=self.is_planqk
        )
        path.write_text(html)
