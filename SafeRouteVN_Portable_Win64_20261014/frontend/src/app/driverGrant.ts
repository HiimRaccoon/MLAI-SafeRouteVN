/** Decode only grants for this exact server; never send a credential to a supplied URL. */
export function decodeDriverGrant(raw: string, origin: string): string {
  const code = raw.trim();
  if (!/^SRVN2\.[A-Za-z0-9_-]+$/.test(code) || code.length > 4096) throw new Error('Hãy dán toàn bộ mã SRVN2 do người điều phối cấp.');
  let grant: {origin?: unknown; token?: unknown};
  try { grant = JSON.parse(atob(code.slice(6).replace(/-/g, '+').replace(/_/g, '/'))); }
  catch { throw new Error('Mã đăng nhập không hợp lệ. Hãy sao chép lại toàn bộ mã.'); }
  if (!grant || typeof grant.origin !== 'string' || !/^https:\/\/[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.tailbe6594\.ts\.net$/.test(grant.origin) || grant.origin !== origin)
    throw new Error('Mã này thuộc máy chủ khác. Hãy lấy mã của máy chủ MLAI_7.');
  if (typeof grant.token !== 'string' || !/^[A-Za-z0-9_-]{32,128}$/.test(grant.token)) throw new Error('Mã đăng nhập không hợp lệ.');
  return grant.token;
}
