// Collect current prices for the product pages listed in urls.txt
// usage: node scrape_prices.js [urls.txt] > raw/prices-$(date +%F).jsonl
const fs = require('fs');
const axios = require('axios');
const cheerio = require('cheerio');

const listFile = process.argv[2] || 'urls.txt';
const urls = fs.readFileSync(listFile, 'utf8').split('\n').map(s => s.trim()).filter(Boolean);

async function scrape(url) {
  const { data } = await axios.get(url, { timeout: 15000, headers: { 'User-Agent': 'price-tracker/0.3' } });
  const $ = cheerio.load(data);
  const price = $('[data-testid="price"]').first().text().replace(/[^0-9.]/g, '');
  return { url, title: $('h1').first().text().trim(), price: Number(price), scrapedAt: new Date().toISOString() };
}

(async () => {
  for (const url of urls) {
    try {
      console.log(JSON.stringify(await scrape(url)));
    } catch (err) {
      console.error(`failed ${url}: ${err.message}`);
    }
    await new Promise(r => setTimeout(r, 2000)); // be polite
  }
})();
