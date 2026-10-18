package vn.saferoute.driver;

/** Credentials are sent only to an exact device origin in the operator's tailnet. */
public final class FunnelPolicy {
    private FunnelPolicy() {}
    public static boolean validOrigin(String origin) {
        return origin != null && origin.matches("https://[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\\.tailbe6594\\.ts\\.net");
    }
    public static boolean validToken(String token) {
        return token != null && token.matches("[A-Za-z0-9_-]{32,128}");
    }
}
