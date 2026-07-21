use anyhow::Result;

pub fn run() -> Result<()> {
    // OTA is implemented in the Python reference (nff/nff/commands/ota.py + tools/ota_client.py):
    // a thin client over the platform's /api/ota/* endpoints (deploy/status/list/devices).
    // Port to Rust pending — until then the shipped binary points users at the Python CLI so the
    // two don't silently diverge (see nff/CLAUDE.md "land features in BOTH").
    println!(
        "nff ota is implemented in the Python CLI but not yet ported to this Rust build.\n\
         Run it via the Python package (`python -m nff ota --help`); Rust port pending."
    );
    Ok(())
}
