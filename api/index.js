// ============================================
//   BRONX API - All in One
//   Developer: @BRONX_ULTRA
// ============================================

const BASE_API = "https://numinfotitan.vercel.app/search?key=TITANKENG&num=";

// ---------- CORS ----------
function setCors(res) {
  res.setHeader("Access-Control-Allow-Origin", "*");
  res.setHeader("Access-Control-Allow-Methods", "GET, OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "Content-Type");
}

// ---------- Fetch TITAN ----------
async function fetchTitan(number) {
  const r = await fetch(BASE_API + number);
  return await r.json();
}

// ---------- Brand wrapper ----------
function brand(obj) {
  return {
    brand: "BRONX",
    developer: "@BRONX_ULTRA",
    ...obj
  };
}

// ============================================
//   MAIN HANDLER
// ============================================
export default async function handler(req, res) {
  setCors(res);
  if (req.method === "OPTIONS") return res.status(200).end();

  const { pathname, searchParams } = new URL(req.url, "http://x");

  // =========================================
  // ROUTE 1: /search?num=XXXXXXXXXX
  // SAB KUCH SHOW — bas aadharNumber HIDE
  // =========================================
  if (pathname.includes("search")) {
    const num = searchParams.get("num");

    if (!num || !/^\d{10}$/.test(num)) {
      return res.status(400).json(
        brand({ success: false, error: "10 digit number required" })
      );
    }

    try {
      const data = await fetchTitan(num); // ✅ No 91 prefix

      const results = (data.results || []).map(item => {
        const { aadharNumber, ...rest } = item;

        if (rest.connected_numbers) {
          rest.connected_numbers = rest.connected_numbers.filter(
            c => c.field !== "aadharNumber"
          );
        }

        return rest;
      });

      return res.status(200).json(
        brand({
          success: true,
          query: num,
          count: results.length,
          results
        })
      );
    } catch (e) {
      return res.status(500).json(
        brand({ success: false, error: "API fetch failed" })
      );
    }
  }

  // =========================================
  // ROUTE 2: /adhar?adhar=XXXXXXXXXX
  // ONLY aadharNumber SHOW
  // =========================================
  if (pathname.includes("adhar")) {
    const adhar = searchParams.get("adhar");

    if (!adhar || !/^\d{10}$/.test(adhar)) {
      return res.status(400).json(
        brand({ success: false, error: "10 digit number required" })
      );
    }

    try {
      const data = await fetchTitan(adhar); // ✅ No 91 prefix

      const list = (data.results || [])
        .map(item => item.aadharNumber)
        .filter(Boolean);

      const unique = [...new Set(list)];
      const results = unique.map(a => ({ aadharNumber: a }));

      return res.status(200).json(
        brand({
          success: true,
          query: adhar,
          count: results.length,
          results
        })
      );
    } catch (e) {
      return res.status(500).json(
        brand({ success: false, error: "API fetch failed" })
      );
    }
  }

  // ---------- DEFAULT ----------
  return res.status(200).json(
    brand({
      success: true,
      message: "BRONX API Running 🚀",
      endpoints: {
        search: "/search?num=9105990680",
        adhar: "/adhar?adhar=9105990680"
      }
    })
  );
}
