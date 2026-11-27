package ai.safestop.app

import android.Manifest
import android.annotation.SuppressLint
import android.content.ActivityNotFoundException
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.net.http.SslError
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import android.util.Log
import android.webkit.CookieManager
import android.webkit.GeolocationPermissions
import android.webkit.PermissionRequest
import android.webkit.SslErrorHandler
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Toast
import androidx.activity.OnBackPressedCallback
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import androidx.webkit.WebSettingsCompat
import androidx.webkit.WebViewFeature

class MainActivity : AppCompatActivity() {

    private lateinit var webView: WebView
    private lateinit var allowedOriginUri: Uri

    private var pendingWebPermissionRequest: PermissionRequest? = null
    private var pendingGeolocationOrigin: String? = null
    private var pendingGeolocationCallback: GeolocationPermissions.Callback? = null

    private companion object {
        const val TAG = "SafeStopWebView"
        const val CAMERA_PERMISSION_REQUEST_CODE = 1001
        const val LOCATION_PERMISSION_REQUEST_CODE = 1002
        val DEV_ALLOWED_HOSTS = setOf("10.0.2.2", "localhost", "127.0.0.1")
        val DEV_ALLOWED_PORTS = setOf(-1, 5000, 5001, 8000, 8080)
    }

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        webView = WebView(this)
        setContentView(webView)

        val launchUri = resolveTrustedLaunchUri(intent?.getStringExtra("SERVER_URL"))
        allowedOriginUri = launchUri

        configureWebViewSettings()
        configureWebClients()
        configureBackNavigation()

        webView.loadUrl(launchUri.toString())
    }

    @SuppressLint("SetJavaScriptEnabled")
    private fun configureWebViewSettings() {
        val settings = webView.settings
        settings.javaScriptEnabled = true
        settings.domStorageEnabled = true
        settings.databaseEnabled = false
        settings.setGeolocationEnabled(true)
        settings.mediaPlaybackRequiresUserGesture = false

        // Disable filesystem, content-provider, mixed content, and third-party cookie exposure
        settings.allowFileAccess = false
        settings.allowContentAccess = false
        @Suppress("DEPRECATION")
        settings.allowFileAccessFromFileURLs = false
        @Suppress("DEPRECATION")
        settings.allowUniversalAccessFromFileURLs = false
        settings.mixedContentMode = WebSettings.MIXED_CONTENT_NEVER_ALLOW
        settings.blockNetworkImage = false

        if (WebViewFeature.isFeatureSupported(WebViewFeature.SAFE_BROWSING_ENABLE)) {
            WebSettingsCompat.setSafeBrowsingEnabled(settings, true)
        } else if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            settings.safeBrowsingEnabled = true
        }

        val cookieManager = CookieManager.getInstance()
        cookieManager.setAcceptCookie(true)
        cookieManager.setAcceptThirdPartyCookies(webView, false)
    }

    private fun configureWebClients() {
        webView.webChromeClient = object : WebChromeClient() {
            override fun onGeolocationPermissionsShowPrompt(
                origin: String,
                callback: GeolocationPermissions.Callback
            ) {
                val originUri = runCatching { Uri.parse(origin) }.getOrNull()
                if (!isSameTrustedOrigin(originUri)) {
                    Log.w(TAG, "Denied geolocation prompt from untrusted origin.")
                    callback.invoke(origin, false, false)
                    return
                }

                if (hasAnyLocationPermission()) {
                    // Do not retain permanently in WebView store so live revocation takes effect
                    callback.invoke(origin, true, false)
                    return
                }

                clearPendingGeolocationRequest()
                pendingGeolocationOrigin = origin
                pendingGeolocationCallback = callback
                ActivityCompat.requestPermissions(
                    this@MainActivity,
                    arrayOf(
                        Manifest.permission.ACCESS_FINE_LOCATION,
                        Manifest.permission.ACCESS_COARSE_LOCATION
                    ),
                    LOCATION_PERMISSION_REQUEST_CODE
                )
            }

            override fun onPermissionRequest(request: PermissionRequest) {
                if (!isSameTrustedOrigin(request.origin)) {
                    Log.w(TAG, "Denied WebView permission request from untrusted origin.")
                    request.deny()
                    return
                }

                val requestedResources = request.resources ?: emptyArray()
                if (!requestedResources.contains(PermissionRequest.RESOURCE_VIDEO_CAPTURE)) {
                    Log.w(TAG, "Denied unsupported or unrequested WebView permission resources.")
                    request.deny()
                    return
                }

                if (ContextCompat.checkSelfPermission(
                        this@MainActivity,
                        Manifest.permission.CAMERA
                    ) == PackageManager.PERMISSION_GRANTED
                ) {
                    request.grant(arrayOf(PermissionRequest.RESOURCE_VIDEO_CAPTURE))
                    return
                }

                pendingWebPermissionRequest?.deny()
                pendingWebPermissionRequest = request
                ActivityCompat.requestPermissions(
                    this@MainActivity,
                    arrayOf(Manifest.permission.CAMERA),
                    CAMERA_PERMISSION_REQUEST_CODE
                )
            }

            override fun onPermissionRequestCanceled(request: PermissionRequest) {
                if (pendingWebPermissionRequest == request) {
                    pendingWebPermissionRequest = null
                }
            }
        }

        webView.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(
                view: WebView?,
                request: WebResourceRequest?
            ): Boolean {
                val uri = request?.url ?: return true
                if (isSameTrustedOrigin(uri)) {
                    return false
                }

                if (request.isForMainFrame && isSafeExternalHttpsUri(uri)) {
                    openExternalBrowser(uri)
                } else {
                    Log.w(TAG, "Blocked navigation to untrusted URL scheme/origin inside WebView.")
                }
                return true
            }

            override fun onReceivedSslError(
                view: WebView?,
                handler: SslErrorHandler?,
                error: SslError?
            ) {
                handler?.cancel()
                Log.e(TAG, "Cancelled WebView load due to SSL certificate error.")
                if (!isFinishing && !isDestroyed) {
                    AlertDialog.Builder(this@MainActivity)
                        .setTitle("Secure Connection Required")
                        .setMessage("SafeStop could not verify the server's HTTPS certificate. Loading was cancelled for your safety.")
                        .setPositiveButton(android.R.string.ok, null)
                        .show()
                }
            }
        }
    }

    private fun configureBackNavigation() {
        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                if (webView.canGoBack()) {
                    webView.goBack()
                } else {
                    isEnabled = false
                    onBackPressedDispatcher.onBackPressed()
                }
            }
        })
    }

    private fun resolveTrustedLaunchUri(rawServerUrl: String?): Uri {
        val productionUri = Uri.parse(BuildConfig.PRODUCTION_SERVER_URL)
        if (rawServerUrl.isNullOrBlank()) {
            return productionUri
        }

        val candidate = runCatching { Uri.parse(rawServerUrl.trim()) }.getOrNull()
        if (candidate != null && isAllowedServerUri(candidate)) {
            return candidate
        }

        Log.w(TAG, "Rejected untrusted SERVER_URL intent extra; falling back to production origin.")
        Toast.makeText(
            this,
            "Untrusted server URL rejected. Using verified SafeStop origin.",
            Toast.LENGTH_LONG
        ).show()
        return productionUri
    }

    private fun isAllowedServerUri(uri: Uri): Boolean {
        val scheme = uri.scheme?.lowercase() ?: return false
        val host = uri.host?.lowercase() ?: return false
        if (!uri.userInfo.isNullOrEmpty()) {
            return false
        }

        val prodUri = Uri.parse(BuildConfig.PRODUCTION_SERVER_URL)
        val prodHost = prodUri.host?.lowercase() ?: return false
        if (scheme == "https" && host == prodHost && normalizedPort(uri) == normalizedPort(prodUri)) {
            return true
        }

        if (BuildConfig.DEBUG && BuildConfig.ALLOW_DEV_SERVER_URL) {
            if (scheme == "http" && host in DEV_ALLOWED_HOSTS && uri.port in DEV_ALLOWED_PORTS) {
                return true
            }
        }
        return false
    }

    private fun isSameTrustedOrigin(uri: Uri?): Boolean {
        if (uri == null || !::allowedOriginUri.isInitialized) {
            return false
        }
        if (!uri.userInfo.isNullOrEmpty()) {
            return false
        }
        val scheme = uri.scheme?.lowercase() ?: return false
        val host = uri.host?.lowercase() ?: return false
        val allowedScheme = allowedOriginUri.scheme?.lowercase() ?: return false
        val allowedHost = allowedOriginUri.host?.lowercase() ?: return false
        return scheme == allowedScheme &&
            host == allowedHost &&
            normalizedPort(uri) == normalizedPort(allowedOriginUri)
    }

    private fun normalizedPort(uri: Uri): Int {
        if (uri.port != -1) return uri.port
        return when (uri.scheme?.lowercase()) {
            "https" -> 443
            "http" -> 80
            else -> -1
        }
    }

    private fun isSafeExternalHttpsUri(uri: Uri): Boolean {
        return uri.scheme.equals("https", ignoreCase = true) &&
            !uri.host.isNullOrBlank() &&
            uri.userInfo.isNullOrEmpty()
    }

    private fun openExternalBrowser(uri: Uri) {
        val externalIntent = Intent(Intent.ACTION_VIEW, uri).apply {
            addCategory(Intent.CATEGORY_BROWSABLE)
        }
        try {
            startActivity(externalIntent)
        } catch (_: ActivityNotFoundException) {
            Toast.makeText(this, "No external browser found to open link.", Toast.LENGTH_SHORT).show()
        }
    }

    private fun hasAnyLocationPermission(): Boolean {
        return ContextCompat.checkSelfPermission(this, Manifest.permission.ACCESS_FINE_LOCATION) == PackageManager.PERMISSION_GRANTED ||
            ContextCompat.checkSelfPermission(this, Manifest.permission.ACCESS_COARSE_LOCATION) == PackageManager.PERMISSION_GRANTED
    }

    private fun clearPendingGeolocationRequest() {
        val origin = pendingGeolocationOrigin
        val callback = pendingGeolocationCallback
        pendingGeolocationOrigin = null
        pendingGeolocationCallback = null
        if (origin != null && callback != null) {
            callback.invoke(origin, false, false)
        }
    }

    override fun onRequestPermissionsResult(
        requestCode: Int,
        permissions: Array<out String>,
        grantResults: IntArray
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)

        when (requestCode) {
            CAMERA_PERMISSION_REQUEST_CODE -> {
                val request = pendingWebPermissionRequest
                pendingWebPermissionRequest = null
                val cameraGranted = ContextCompat.checkSelfPermission(
                    this,
                    Manifest.permission.CAMERA
                ) == PackageManager.PERMISSION_GRANTED
                if (request != null) {
                    if (cameraGranted && isSameTrustedOrigin(request.origin)) {
                        request.grant(arrayOf(PermissionRequest.RESOURCE_VIDEO_CAPTURE))
                    } else {
                        request.deny()
                    }
                }
                if (!cameraGranted) {
                    val permanentlyDenied = !ActivityCompat.shouldShowRequestPermissionRationale(
                        this,
                        Manifest.permission.CAMERA
                    )
                    handlePermissionDenied(
                        permanentlyDenied,
                        "Camera Permission Required",
                        "Camera access is needed for student face verification. Enable Camera in App Settings."
                    )
                }
            }

            LOCATION_PERMISSION_REQUEST_CODE -> {
                val origin = pendingGeolocationOrigin
                val callback = pendingGeolocationCallback
                pendingGeolocationOrigin = null
                pendingGeolocationCallback = null
                val locationGranted = hasAnyLocationPermission()
                if (origin != null && callback != null) {
                    val originUri = runCatching { Uri.parse(origin) }.getOrNull()
                    callback.invoke(origin, locationGranted && isSameTrustedOrigin(originUri), false)
                }
                if (!locationGranted) {
                    val permanentlyDenied =
                        !ActivityCompat.shouldShowRequestPermissionRationale(this, Manifest.permission.ACCESS_FINE_LOCATION) &&
                        !ActivityCompat.shouldShowRequestPermissionRationale(this, Manifest.permission.ACCESS_COARSE_LOCATION)
                    handlePermissionDenied(
                        permanentlyDenied,
                        "Location Permission Required",
                        "Location access is needed for live driver GPS tracking. Enable Location in App Settings."
                    )
                }
            }
        }
    }

    private fun handlePermissionDenied(permanentlyDenied: Boolean, title: String, settingsMessage: String) {
        if (permanentlyDenied && !isFinishing && !isDestroyed) {
            AlertDialog.Builder(this)
                .setTitle(title)
                .setMessage(settingsMessage)
                .setPositiveButton("Open Settings") { _, _ ->
                    val settingsIntent = Intent(
                        Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                        Uri.fromParts("package", packageName, null)
                    )
                    try {
                        startActivity(settingsIntent)
                    } catch (_: ActivityNotFoundException) {
                        Toast.makeText(this, settingsMessage, Toast.LENGTH_LONG).show()
                    }
                }
                .setNegativeButton(android.R.string.cancel, null)
                .show()
        } else {
            Toast.makeText(this, settingsMessage, Toast.LENGTH_LONG).show()
        }
    }

    override fun onResume() {
        super.onResume()
        webView.onResume()
    }

    override fun onPause() {
        webView.onPause()
        super.onPause()
    }

    override fun onDestroy() {
        pendingWebPermissionRequest?.deny()
        pendingWebPermissionRequest = null
        clearPendingGeolocationRequest()
        webView.destroy()
        super.onDestroy()
    }
}
