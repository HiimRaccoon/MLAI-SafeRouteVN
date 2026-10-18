package vn.saferoute.driver;

import android.app.Activity;
import android.os.Bundle;
import android.net.Uri;
import android.net.http.SslError;
import android.graphics.Color;
import android.view.View;
import android.webkit.*;
import android.widget.*;

/** One interactive web login. No credentials in native code, URLs or preferences. */
public final class MainActivity extends Activity {
    private static final String ORIGIN = "https://laptop-s2lvd91p-1.tailbe6594.ts.net";
    private WebView web;
    private LinearLayout root, errorView;
    private boolean failed;
    private boolean trusted(String address) {
        Uri u = Uri.parse(address);
        return "https".equals(u.getScheme()) && Uri.parse(ORIGIN).getHost().equals(u.getHost()) && u.getPort() == -1 && u.getUserInfo() == null;
    }
    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        if ((getApplicationInfo().flags & android.content.pm.ApplicationInfo.FLAG_DEBUGGABLE) == 0)
            getWindow().setFlags(android.view.WindowManager.LayoutParams.FLAG_SECURE, android.view.WindowManager.LayoutParams.FLAG_SECURE);
        root = new LinearLayout(this); root.setOrientation(LinearLayout.VERTICAL); root.setBackgroundColor(Color.rgb(238,244,248));
        root.setOnApplyWindowInsetsListener((v,i) -> { v.setPadding(i.getSystemWindowInsetLeft(),i.getSystemWindowInsetTop(),i.getSystemWindowInsetRight(),i.getSystemWindowInsetBottom()); return i; });
        web = new WebView(this); web.setSaveEnabled(false);
        if ((getApplicationInfo().flags & android.content.pm.ApplicationInfo.FLAG_DEBUGGABLE) != 0) WebView.setWebContentsDebuggingEnabled(true);
        WebSettings s = web.getSettings(); s.setJavaScriptEnabled(true); s.setDomStorageEnabled(true);
        s.setUserAgentString(s.getUserAgentString()+" SafeRouteVNDriverFunnel/2.3.2");
        s.setAllowFileAccess(false); s.setAllowContentAccess(false); s.setGeolocationEnabled(false);
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW); s.setJavaScriptCanOpenWindowsAutomatically(false);
        CookieManager.getInstance().setAcceptThirdPartyCookies(web,false);
        web.setWebChromeClient(new WebChromeClient() { @Override public void onPermissionRequest(PermissionRequest r) {r.deny();} });
        web.setWebViewClient(new WebViewClient() {
            @Override public boolean shouldOverrideUrlLoading(WebView v,String url) {return !trusted(url);}
            @Override public boolean shouldOverrideUrlLoading(WebView v,WebResourceRequest r) {return !trusted(r.getUrl().toString());}
            @Override public void onReceivedSslError(WebView v,SslErrorHandler h,SslError e) {
                h.cancel();
                // A failed map resource must not destroy an authenticated Driver session.
                if (trusted(e.getUrl())) showError("Không thể xác minh HTTPS máy chủ.");
            }
            @Override public void onReceivedError(WebView v,WebResourceRequest r,WebResourceError e) {
                if(r.isForMainFrame()) showError("Không kết nối được máy chủ. Kiểm tra mạng và yêu cầu admin mở SafeRouteVN.");
            }
            @Override public void onReceivedHttpError(WebView v,WebResourceRequest r,WebResourceResponse e) {
                if(r.isForMainFrame() && e.getStatusCode()>=400) showError("Máy chủ chưa sẵn sàng. Vui lòng thử lại.");
            }
            @Override public void onPageFinished(WebView v,String url) {if(!failed && trusted(url)) web.setVisibility(View.VISIBLE);}
        });
        root.addView(web,new LinearLayout.LayoutParams(-1,0,1));
        errorView = new LinearLayout(this); errorView.setOrientation(LinearLayout.VERTICAL); errorView.setPadding(dp(24),dp(60),dp(24),dp(24));
        TextView title=new TextView(this); title.setText("SafeRouteVN Driver"); title.setTextSize(25); title.setTextColor(Color.rgb(23,48,74)); errorView.addView(title);
        TextView message=new TextView(this); message.setTag("message"); message.setTextSize(16); message.setPadding(0,dp(20),0,dp(20)); errorView.addView(message);
        Button retry=new Button(this); retry.setText("Kết nối lại"); retry.setOnClickListener(v -> load()); errorView.addView(retry);
        errorView.setVisibility(View.GONE); root.addView(errorView); setContentView(root); load();
    }
    private int dp(int n) {return Math.round(n*getResources().getDisplayMetrics().density);}
    private void load() {failed=false; errorView.setVisibility(View.GONE); web.setVisibility(View.VISIBLE); web.loadUrl(ORIGIN+"/driver");}
    private void showError(String text) {failed=true; web.setVisibility(View.GONE); ((TextView)errorView.findViewWithTag("message")).setText(text); errorView.setVisibility(View.VISIBLE);}
    @Override public void onBackPressed() {
        web.evaluateJavascript("(()=>{if(document.querySelector('.drv-page--map-expanded')){window.dispatchEvent(new Event('saferoute:back'));return true;}return false;})()",result -> {if(!"true".equals(result)) MainActivity.super.onBackPressed();});
    }
    @Override protected void onResume() {
        super.onResume();
        if (web != null) {
            web.onResume();
            web.evaluateJavascript("window.dispatchEvent(new Event('saferoute:resume'));", null);
        }
    }
    @Override protected void onPause() { if (web != null) web.onPause(); super.onPause(); }
    @Override protected void onDestroy() {web.stopLoading(); root.removeView(web); web.destroy(); super.onDestroy();}
}
