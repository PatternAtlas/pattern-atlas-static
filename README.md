# Static Site Generator for the Pattern Atlas Data

Generate static html pages from pattern data retrieved from the PatternAtlas API.

## Usage

```bash
# compile the documentation
poetry run python -m static_patternatlas --atlas-url="http://localhost:1977/patternatlas" --out html
# compile the documentation for planqk
poetry run python -m static_patternatlas --atlas-url="https://patternatlas.planqk.de/patternatlas" --planqk --out html

# start a dev server on port 8000
poetry run python -m http.server --directory html
```

## Install Dependencies

This tool uses the `poetry` package manager.

```bash
poetry run install
```

Linting and formatting is done with `ruff`.
