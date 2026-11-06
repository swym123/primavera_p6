# primavera_p6

Primavera P6 PDS Table Selector and Reporting View Metadata Tool.

## Getting Started (Local)

1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
2. Copy `.env.example` to `.env` and configure your settings:
   ```bash
   cp .env.example .env
   ```
3. Run the application:
   ```bash
   python pds_table_selector.py
   ```
4. Access the web interface at `http://localhost:8765`.

## Deploying to Render

This application is ready to deploy on [Render](https://render.com):

1. Click **New +** → **Web Service** on Render.
2. Connect your GitHub repository (`swym123/primavera_p6`).
3. Set the service settings:
   - **Runtime**: `Python 3`
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `python pds_table_selector.py`
   - **Plan**: `Free`
4. Set optional Environment Variables in the Render dashboard:
   - `AUTO_OPEN_BROWSER` = `false`
   - `PDS_ORIGIN` = *(your PDS endpoint, defaults to Oracle Cloud if omitted)*
   - `PDS_USERNAME` / `PDS_PASSWORD` = *(optional server-level fallback credentials)*
