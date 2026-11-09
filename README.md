# Battery Pilot app repository

A Home Assistant app (add-on) repository with one app, **Battery Pilot**: it plans and
steers a Solis home battery against your contract, prices, solar forecast and tree
shade, and compares fixed with dynamic contracts.

Add it in Home Assistant: Settings → Apps (Add-ons) → store → ⋮ → Repositories, paste
`https://github.com/Azer1652/hassSolarBattery`, then install **Battery Pilot**.

The repository holds no personal data: your entities and rates live in the app's
settings on your own Home Assistant.

## Development

```bash
pip install -r requirements-dev.txt
pytest
```
