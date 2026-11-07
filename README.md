# Prusa G-code Viewer

A Home Assistant integration and dashboard card that shows a 3D view of what your Prusa printer is printing. Layers already printed are solid and the rest are faint, following the printer's Z height as the print goes on.

The card draws the toolpaths from the print's G-code, as PrusaSlicer's preview does. Each tool is drawn in its filament colour, so multi-tool prints (INDX, XL, MMU) look the way they will come off the bed.

## How it works

- When the card opens, the integration asks the printer (through PrusaLink) which file it is printing.
- **New print:** it downloads the G-code once (plain `.gcode` or binary `.bgcode`) and builds a compact model of the extrusion paths. The model is saved in `/config/.cache/prusa_gcode_viewer`, which isn't included in backups. The last 5 models are kept.
- **Same print again:** the saved model is served straight away. The printer isn't asked for the file again.
- **Nothing printing:** the card shows the last print.
- **Dropped file:** a G-code or 3MF file dropped on the card is used without asking the printer for it. See [Showing a file](#showing-a-file).

The first view of a new print downloads the file from the printer. That takes from a few seconds to a few minutes, depending on the file size and the printer's network. The card shows the download progress.

## Requirements

- The [PrusaLink](https://www.home-assistant.io/integrations/prusalink/) integration, set up for your printer. The viewer uses its address and login, so there's nothing new to enter.

## Install

**HACS (recommended):** HACS → ⋮ → **Custom repositories** → add `https://github.com/iDaddyCz/ha-prusa-gcode-viewer` as type **Integration**. Install **Prusa G-code Viewer**, then restart Home Assistant.

**Manual:** copy `custom_components/prusa_gcode_viewer` to `/config/custom_components/prusa_gcode_viewer`, then restart Home Assistant.

Then go to **Settings → Devices & services → Add integration → Prusa G-code Viewer**. With one PrusaLink printer it's set up at once; with several, pick one.

The integration loads the card itself, so you don't need to add a dashboard resource.

## The card

```yaml
type: custom:prusa-gcode-viewer-card
```

| Option     | Default | Description |
|------------|---------|-------------|
| `height`   | `360`   | Card height in pixels. |
| `color_by` | `tool`  | `tool`: filament colours, with inner features darker. `type`: PrusaSlicer's feature colours (perimeters, infill, supports…). |
| `walls`    | `false` | Start with only the outer walls shown. The **Walls** button toggles this. |
| `entry_id` | –       | Which viewer to use when several printers are set up. |

To skip the slow download from the printer, drag the print's file onto the card, or use the **Open** button on a phone. See [Showing a file](#showing-a-file).

Drag to rotate, scroll or pinch to zoom, and right-drag or two-finger drag to pan. **Reset** goes back to the starting view. On a phone, tap the card first to rotate it, so the page still scrolls normally; **Done** releases it.

## Showing a file

The card accepts `.gcode`, `.bgcode` and `.3mf` files of up to 256 MB. Home Assistant builds the model, as it does for a file from the printer, so it needs no extra software and every device sees the result.

- **While printing:** the file replaces the model of the current print and any download from the printer stops. The card still follows the printer's Z height. Drop another file to replace it.
- **While not printing:** the file is shown until the next print starts. It's kept with the other saved models, but isn't linked to that print, so the next print downloads its own file as usual.

A 3MF slicer project usually holds meshes, not toolpaths, so the card cuts each mesh into an outline per layer, labelled "outlines only". This shows the shape, filament colours, painted areas and layers, but no infill, supports, brim or wipe tower. Layers use the project's layer height, without variable layer heights, so the layer count can differ from the sliced print. A 3MF with sliced G-code in it (Bambu Studio, OrcaSlicer) shows that G-code instead. For exact toolpaths, drop the exported G-code.

## Development

```bash
pip install -r requirements_test.txt
pytest -q
```

The card is in `frontend/src/card.js`. After changing it, rebuild the bundle that the integration serves, and commit that bundle too:

```bash
cd frontend && npm ci && npm run build
```

`tests/fixtures/star.gcode.gz` was sliced in PrusaSlicer for a CORE One INDX from the model made by `scripts/make_fixture_model.py`.
