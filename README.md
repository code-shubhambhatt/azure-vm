# VM Flask Live (No Local Files)

This app builds VM UI options and computes VM cost directly from live Azure calculator endpoints.

## Endpoints used

- `/api/v2/pricing/categories/calculator/?culture=en-in`
- `/api/v4/pricing/virtual-machines/metadata/`
- `/api/v4/pricing/virtual-machines/calculator/{region}/?culture={culture}`

## Run

```powershell
python -m venv .venv
.\.venv\Scripts\activate
pip install -r vm_flask_live\requirements.txt
python vm_flask_live\app.py
```

Open: `http://127.0.0.1:5000`




