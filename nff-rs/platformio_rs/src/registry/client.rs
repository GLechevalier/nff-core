//! Port of `platformio/registry/client.py` (`RegistryClient`) — the read-only
//! `/v3/*` endpoints the package manager uses. Auth/private packages are out of
//! scope (public access only), so `x_with_authorization` is always false.

use serde_json::Value;

use crate::http::HttpClient;
use crate::package::error::{PackageError, Result};
use crate::REGISTRY_MIRROR_HOSTS;

/// `platformio.registry.client.RegistryClient`.
pub struct RegistryClient {
    http: HttpClient,
}

impl Default for RegistryClient {
    fn default() -> Self {
        Self::new()
    }
}

impl RegistryClient {
    #[must_use]
    pub fn new() -> Self {
        let endpoints = REGISTRY_MIRROR_HOSTS.iter().map(|h| format!("https://api.{h}")).collect();
        Self { http: HttpClient::new(endpoints, true) }
    }

    /// `RegistryClient.get_package` — `GET /v3/packages/{owner}/{type}/{name}`.
    /// Returns `None` on HTTP 404.
    pub fn get_package(
        &self,
        typex: &str,
        owner: &str,
        name: &str,
        version: Option<&str>,
    ) -> Result<Option<Value>> {
        let path = format!("/v3/packages/{}/{typex}/{}", owner.to_lowercase(), name.to_lowercase());
        let params: Vec<(String, String)> = version
            .map(|v| vec![("version".to_string(), v.to_string())])
            .unwrap_or_default();
        match self.http.fetch_json(&path, &params) {
            Ok(v) => Ok(Some(v)),
            Err(PackageError::Http { status: Some(404), .. }) => Ok(None),
            Err(e) => Err(e),
        }
    }

    /// `RegistryClient.list_packages` — `GET /v3/search`. `qualifiers` are
    /// `(plural-name, value)` pairs (`ids`/`names`/`owners`/`types`/…), rendered
    /// as `singular:"value"` search tokens.
    pub fn list_packages(&self, qualifiers: &[(&str, String)], query: Option<&str>) -> Result<Value> {
        let mut tokens: Vec<String> = Vec::new();
        for (name, value) in qualifiers {
            let singular = name.strip_suffix('s').unwrap_or(name);
            tokens.push(format!("{singular}:\"{value}\""));
        }
        if let Some(q) = query {
            tokens.push(q.to_string());
        }
        let params = vec![("query".to_string(), tokens.join(" "))];
        self.http.fetch_json("/v3/search", &params)
    }
}
