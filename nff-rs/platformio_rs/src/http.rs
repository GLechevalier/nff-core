//! Port of the `platformio/http.py` slice the registry client needs: an
//! `HttpClient` with endpoint/mirror failover over a blocking `reqwest` client,
//! plus JSON fetching with the upstream status-code contract.
//!
//! Documented deviations (this layer is network-only and can't be unit-tested
//! offline): the `urllib3` `Retry` on the 413/429/5xx status-forcelist, the
//! `ContentCache` disk cache, the `ensure_internet_on` pre-check, and bearer
//! authentication (`x_with_authorization`) are omitted — only public,
//! unauthenticated registry access is supported. Failover across endpoints
//! (on connection errors) is preserved.

use std::time::Duration;

use serde_json::Value;

use crate::package::error::{PackageError, Result};

const CONNECT_TIMEOUT: Duration = Duration::from_secs(10);
const USER_AGENT: &str = concat!("platformio_rs/", env!("CARGO_PKG_VERSION"));

/// `platformio.http.HTTPClient` — a set of interchangeable base URLs (mirrors).
pub struct HttpClient {
    endpoints: Vec<String>,
    follow_redirects: bool,
}

impl HttpClient {
    #[must_use]
    pub fn new(endpoints: Vec<String>, follow_redirects: bool) -> Self {
        Self { endpoints, follow_redirects }
    }

    fn build_client(&self) -> Result<reqwest::blocking::Client> {
        let redirect = if self.follow_redirects {
            reqwest::redirect::Policy::default()
        } else {
            reqwest::redirect::Policy::none()
        };
        reqwest::blocking::Client::builder()
            .user_agent(USER_AGENT)
            .connect_timeout(CONNECT_TIMEOUT)
            .redirect(redirect)
            .build()
            .map_err(|e| PackageError::Http { message: e.to_string(), status: None })
    }

    /// `HTTPClient.send_request` — try each endpoint until one connects
    /// (mirrors the `_next_session` failover). Non-2xx responses are returned to
    /// the caller (they decide), matching how upstream inspects status codes.
    pub fn send_request(
        &self,
        method: reqwest::Method,
        path: &str,
        params: &[(String, String)],
    ) -> Result<reqwest::blocking::Response> {
        let client = self.build_client()?;
        let mut last_err = PackageError::Http {
            message: "no registry endpoints configured".to_string(),
            status: None,
        };
        for base in &self.endpoints {
            let url = format!("{}{}", base.trim_end_matches('/'), path);
            let req = client.request(method.clone(), &url).query(params);
            match req.send() {
                Ok(resp) => return Ok(resp),
                Err(e) => last_err = PackageError::Http { message: e.to_string(), status: None },
            }
        }
        Err(last_err)
    }

    /// `HTTPClient.fetch_json_data` (cache omitted). Success codes 200/201/202.
    pub fn fetch_json(&self, path: &str, params: &[(String, String)]) -> Result<Value> {
        let resp = self.send_request(reqwest::Method::GET, path, params)?;
        let status = resp.status().as_u16();
        let text = resp.text().map_err(|e| PackageError::Http { message: e.to_string(), status: Some(status) })?;
        if matches!(status, 200..=202) {
            return serde_json::from_str(&text)
                .map_err(|e| PackageError::Http { message: e.to_string(), status: Some(status) });
        }
        // Prefer a JSON `message` field, else the raw body (upstream contract).
        let message = serde_json::from_str::<Value>(&text)
            .ok()
            .and_then(|v| v.get("message").and_then(Value::as_str).map(str::to_string))
            .unwrap_or(text);
        Err(PackageError::Http { message, status: Some(status) })
    }
}
