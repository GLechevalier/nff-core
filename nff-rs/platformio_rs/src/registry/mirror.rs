//! Port of `platformio/registry/mirror.py` (`RegistryFileMirrorIterator`) — turns
//! a package file's `download_url` into concrete `(Location, sha256)` pairs by
//! following the registry's per-mirror HEAD redirects. The `ContentCache` is
//! omitted (see [`crate::http`]).

use crate::http::HttpClient;
use crate::package::error::Result;
use crate::package::meta::urlparse;
use crate::REGISTRY_MIRROR_HOSTS;

/// `platformio.registry.mirror.RegistryFileMirrorIterator`.
pub struct RegistryFileMirrorIterator {
    path: String,
    http: HttpClient,
    visited_mirrors: Vec<String>,
}

impl RegistryFileMirrorIterator {
    #[must_use]
    pub fn new(download_url: &str) -> Self {
        let (scheme, netloc, path) = urlparse(download_url);
        let mirror = format!("{scheme}://{netloc}");
        let mut endpoints = vec![mirror];
        for host in REGISTRY_MIRROR_HOSTS {
            let endpoint = format!("https://dl.{host}");
            if !endpoints.contains(&endpoint) {
                endpoints.push(endpoint);
            }
        }
        Self { path, http: HttpClient::new(endpoints, false), visited_mirrors: Vec::new() }
    }

    /// Advance to the next mirror. Returns `(Location, sha256)` or `None` when the
    /// mirror chain is exhausted (`StopIteration`).
    pub fn next_mirror(&mut self) -> Result<Option<(String, Option<String>)>> {
        let params: Vec<(String, String)> = if self.visited_mirrors.is_empty() {
            Vec::new()
        } else {
            vec![("bypass".to_string(), self.visited_mirrors.join(","))]
        };
        let resp = self.http.send_request(reqwest::Method::HEAD, &self.path, &params)?;
        let status = resp.status().as_u16();
        let header = |name: &str| resp.headers().get(name).and_then(|v| v.to_str().ok()).map(str::to_string);
        let location = header("location");
        let x_mirror = header("x-pio-mirror");
        let sha256 = header("x-pio-content-sha256");

        let stop = !matches!(status, 302 | 307)
            || location.is_none()
            || x_mirror.is_none()
            || x_mirror.as_ref().is_some_and(|m| self.visited_mirrors.contains(m));
        if stop {
            return Ok(None);
        }
        self.visited_mirrors.push(x_mirror.unwrap());
        Ok(Some((location.unwrap(), sha256)))
    }
}
