import { mkdir, writeFile } from 'node:fs/promises';

const BASE = 'https://tdx.transportdata.tw/api/tourism/service/odata/V2/Tourism';
const { TDX_CLIENT_ID, TDX_CLIENT_SECRET } = process.env;

if (!TDX_CLIENT_ID || !TDX_CLIENT_SECRET) {
  console.error('Missing TDX_CLIENT_ID / TDX_CLIENT_SECRET');
  process.exit(1);
}

// category 必須和 index.html 的 TDX_SOURCES.name 完全一致
const SOURCES = [
  { path: 'Attraction', idField: 'AttractionID', nameField: 'AttractionName',
    category: 'Tourist attractions', icon: 'S', color: '#c96b1a', max: 600 },
  { path: 'Restaurant', idField: 'RestaurantID', nameField: 'RestaurantName',
    category: 'Tourist restaurant', icon: 'R', color: '#2b8c6e', max: 300 },
  { path: 'Event', idField: 'EventID', nameField: 'EventName',
    category: 'Cultural and creative activities', icon: 'A', color: '#9e2a1c', max: 200, isEvent: true },
];

const PAGE_SIZES = [1000, 200, 100, 30];
const RAW_LIMIT = 3000;
const sleep = ms => new Promise(r => setTimeout(r, ms));
const clean = s => String(s ?? '').replace(/<[^>]*>/g, '').replace(/\s+/g, ' ').trim();

async function getToken() {
  const res = await fetch(
    'https://tdx.transportdata.tw/auth/realms/TDXConnect/protocol/openid-connect/token',
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams({
        grant_type: 'client_credentials',
        client_id: TDX_CLIENT_ID,
        client_secret: TDX_CLIENT_SECRET,
      }),
    }
  );
  if (!res.ok) throw new Error('Token HTTP ' + res.status);
  const data = await res.json();
  if (!data.access_token) throw new Error('No access_token');
  return data.access_token;
}

async function fetchPage(src, token, top, skip) {
  const url = `${BASE}/${src.path}?$top=${top}&$skip=${skip}`;
  const res = await fetch(url, { headers: { authorization: `Bearer ${token}` } });
  if (!res.ok) {
    const body = (await res.text()).slice(0, 500);
    return { ok: false, status: res.status, body };
  }
  const json = await res.json();
  return { ok: true, items: Array.isArray(json.value) ? json.value : [] };
}

async function fetchAll(src, token) {
  const seen = new Set();
  const all = [];

  let pageSize = null;
  let first = null;
  for (const size of PAGE_SIZES) {
    const r = await fetchPage(src, token, size, 0);
    if (r.ok) { pageSize = size; first = r.items; break; }
    console.log(`${src.path}: $top=${size} rejected -> HTTP ${r.status} ${r.body}`);
  }
  if (!pageSize) throw new Error(`${src.path}: no page size accepted`);
  console.log(`${src.path}: using $top=${pageSize}`);

  const add = items => {
    let added = 0;
    for (const it of items) {
      const id = it[src.idField];
      if (id && !seen.has(id)) { seen.add(id); all.push(it); added++; }
    }
    return added;
  };

  add(first);
  let skip = first.length;
  while (first.length === pageSize && all.length < RAW_LIMIT) {
    await sleep(300);
    const r = await fetchPage(src, token, pageSize, skip);
    if (!r.ok) {
      console.log(`${src.path}: $skip=${skip} failed -> HTTP ${r.status} ${r.body}`);
      break;
    }
    if (r.items.length === 0 || add(r.items) === 0) break;
    skip += r.items.length;
    if (r.items.length < pageSize) break;
  }
  return all;
}

function convert(src, item) {
  const lat = parseFloat(item.PositionLat);
  const lng = parseFloat(item.PositionLon);
  if (isNaN(lat) || isNaN(lng)) return null;
  if (lat < 21.5 || lat > 26.5 || lng < 118.0 || lng > 122.5) return null;

  const name = clean(item[src.nameField]);
  if (!name) return null;

  if (src.isEvent) {
    if (item.EventStatus === 'EventCancelled') return null;
    const end = item.EndDateTime ? Date.parse(item.EndDateTime) : NaN;
    if (!isNaN(end) && end < Date.now()) return null;
  }

  const a = item.PostalAddress || {};
  const addr = [a.City, a.Town, a.StreetAddress].filter(Boolean).join('');

  return {
    id: item[src.idField],
    name,
    category: src.category,
    icon: src.icon,
    color: src.color,
    lat, lng, addr,
    desc: clean(item.Description).slice(0, 200),
    pic: item.Images?.[0]?.URL || '',
    officialUrl: item.WebsiteUrl || 'https://www.taiwan.net.tw/',
    mapUrl: `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(name + ' ' + addr)}`,
  };
}

async function main() {
  const token = await getToken();
  const output = [];

  for (const src of SOURCES) {
    const raw = await fetchAll(src, token);
    const locs = raw.map(it => convert(src, it)).filter(Boolean)
      .sort((a, b) => a.id.localeCompare(b.id))
      .slice(0, src.max);
    console.log(`${src.path}: raw ${raw.length} -> kept ${locs.length}`);
    if (locs.length === 0) throw new Error(`${src.path} produced 0 locations, abort`);
    output.push(...locs);
  }

  await mkdir('data', { recursive: true });
  await writeFile('data/locations.json', JSON.stringify(output));
  console.log(`Wrote ${output.length} locations`);
}

main().catch(err => {
  console.error('FAILED:', err.message);
  process.exit(1);
});