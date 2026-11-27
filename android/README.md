# SafeStop — Android WebView Shell (`ai.safestop.app`)

This directory contains the Android Studio project for the SafeStop WebView application (`ai.safestop.app`).

## Security & Permission Architecture (Phase 2A)

- **Trusted Production Origin:** Defaults to `https://safestop.onrender.com` (`BuildConfig.PRODUCTION_SERVER_URL`).
- **Debug Local Development Origin:** `SERVER_URL` intent extra permits local HTTP URLs (`http://10.0.2.2`, `http://localhost`, `http://127.0.0.1` on ports `5000`, `5001`, `8000`, `8080`) **only** in `debug` builds (`BuildConfig.DEBUG && BuildConfig.ALLOW_DEV_SERVER_URL`).
- **Release Builds:** Force `usesCleartextTraffic="false"` and reject all `http://` URLs and untrusted hosts/schemes.
- **Navigation Isolation:** Only same-origin URLs matching the validated launch origin load inside the authenticated `WebView`. Legitimate external `https://` links open in the external browser; untrusted schemes (`file:`, `content:`, `javascript:`, `data:`) are blocked.
- **Transport & WebView Hardening:** Disables mixed content (`MIXED_CONTENT_NEVER_ALLOW`), disables file/content access (`allowFileAccess = false`, `allowContentAccess = false`), disables third-party cookies, enables Safe Browsing where supported, cancels SSL certificate errors (`handler?.cancel()`), and defines no custom `JavascriptInterface`.
- **Scoped Runtime Permissions:**
  - `CAMERA` is requested and granted only for `PermissionRequest.RESOURCE_VIDEO_CAPTURE` from the trusted SafeStop origin.
  - `ACCESS_FINE_LOCATION` / `ACCESS_COARSE_LOCATION` are requested and granted only for geolocation prompts from the trusted SafeStop origin (with `retain = false` so live permission revocation is respected).
  - Unsupported or unrequested WebView resources (microphone, DRM, etc.) are denied.
  - Background location and native foreground GPS service permissions are intentionally excluded until Phase 2B.

---

## Build Instructions

Open `android/` in Android Studio or run via Gradle:

```bash
./gradlew assembleDebug
```

Debug builds write `android/app/build/outputs/apk/debug/app-debug.apk`.

---

## Phase 2A Android Manual Verification Checklist

1. **Trusted Production URL (Default / Release & Debug):**
   - Launch `MainActivity` without `SERVER_URL` extra (or with `SERVER_URL="https://safestop.onrender.com"`).
   - Verify SafeStop loads over HTTPS and navigation stays inside the WebView.
2. **Rejection of Untrusted URL (`SERVER_URL` Intent Extra):**
   - Launch with `adb shell am start -n ai.safestop.app/.MainActivity --es SERVER_URL "https://evil.example.com"` or `--es SERVER_URL "http://192.168.1.50:5000"` or `--es SERVER_URL "javascript:alert(1)"`.
   - Verify the toast/warning appears, the untrusted URL does not load, and the WebView falls back to `https://safestop.onrender.com`.
   - In a `release` build, also pass `http://10.0.2.2:5000` and verify it is rejected.
3. **HTTPS Certificate Failure:**
   - Connect a debug build or test device to an HTTPS endpoint presenting an invalid/self-signed/mismatched certificate.
   - Verify `onReceivedSslError` calls `handler?.cancel()`, displays the "Secure Connection Required" alert, and never proceeds or renders the untrusted page.
4. **Camera Permission Granted & Denied:**
   - Open Driver Face Verification on the trusted origin.
   - Grant Camera permission when prompted → verify camera stream initializes cleanly.
   - Deny Camera once → verify clean user prompt; deny permanently ("Don't ask again") → verify App Settings dialog appears without crashing.
   - Revoke Camera in Android Settings and re-open Face Verification → verify the WebView does not treat the prior grant as permanent.
5. **Location Permission Granted & Denied:**
   - Log in as Driver and click **Go Online & Activate Bus** on the trusted origin.
   - Grant Location permission → verify geolocation initializes and dashboard telemetry shows `GPS active (syncing while console is open)` after server confirmation.
   - Deny Location permission → verify driver dashboard displays `GPS permission denied` (never stuck on active).
6. **External Link Handling:**
   - Navigate to or tap an external `https://` link whose host differs from the SafeStop origin.
   - Verify the external URL opens in Android's external browser rather than inside the authenticated SafeStop WebView, and `file://` / `javascript:` links are blocked.
7. **Release Build Cleartext Traffic Disabled:**
   - Build or inspect the merged release manifest (`assembleRelease` / `processReleaseMainManifest`).
   - Confirm `android:usesCleartextTraffic="false"` in the release manifest and `BuildConfig.ALLOW_DEV_SERVER_URL == false`.
