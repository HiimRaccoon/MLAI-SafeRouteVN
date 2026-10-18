package vn.saferoute.driver;

public final class FunnelPolicyTest {
    private static int checks;
    private static void check(boolean ok) { checks++; if (!ok) throw new AssertionError("Check " + checks); }
    public static void main(String[] args) {
        check(FunnelPolicy.validOrigin("https://saferoute.tailbe6594.ts.net"));
        String[] invalid = {"http://saferoute.tailbe6594.ts.net", "https://tailbe6594.ts.net",
            "https://saferoute.tailbe6594.ts.net.evil.com", "https://evil.ts.net",
            "https://saferoute.tailbe6594.ts.net/", "https://saferoute.tailbe6594.ts.net:443",
            "https://user@saferoute.tailbe6594.ts.net", "https://saferoute.tailbe6594.ts.net?token=x",
            "https://-bad.tailbe6594.ts.net", "https://bad-.tailbe6594.ts.net", null};
        for (String origin : invalid) check(!FunnelPolicy.validOrigin(origin));
        check(FunnelPolicy.validToken("abcdefghijklmnopqrstuvwxyz0123456789_-"));
        check(!FunnelPolicy.validToken("short"));
        check(!FunnelPolicy.validToken("abcdefghijklmnopqrstuvwxyz0123456789\n"));
        check(!FunnelPolicy.validToken(null));
        System.out.println("PASS: " + checks + " Funnel origin/token checks");
    }
}
