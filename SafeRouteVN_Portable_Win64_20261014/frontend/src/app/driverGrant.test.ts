import { expect, it } from 'vitest';
import { decodeDriverGrant } from './driverGrant';
const origin = 'https://laptop-s2lvd91p-1.tailbe6594.ts.net';
const token = 'test-only-'.padEnd(48, 'x');
const code = (target: string, value = token) => 'SRVN2.' + btoa(JSON.stringify({origin: target, token: value})).replace(/=/g, '').replace(/\+/g, '-').replace(/\//g, '_');
it('extracts only the bearer from a complete SRVN2 code for the current server', () => {
  expect(decodeDriverGrant('  '+code(origin)+'\n', origin)).toBe(token);
});
it.each(['https://evil.example', 'https://other.tailbe6594.ts.net', origin+'/driver', origin+':443'])('rejects a code for a different or noncanonical origin %s', target => {
  expect(() => decodeDriverGrant(code(target), origin)).toThrow();
});
it.each(['TX01', 'SRVN2.invalid', code(origin, 'short'), code(origin, 'x'.repeat(150))])('rejects malformed grants', value => {
  expect(() => decodeDriverGrant(value, origin)).toThrow();
});
