// node csv_to_json.js in.csv out.json   (same thing as the python one, for the web team)
const fs = require('fs');
const [input, output] = process.argv.slice(2);
const [header, ...lines] = fs.readFileSync(input, 'utf8').trim().split(/\r?\n/);
const keys = header.split(',');
const rows = lines.map(line => {
  const values = line.split(',');
  return Object.fromEntries(keys.map((k, i) => [k, isNaN(values[i]) ? values[i] : Number(values[i])]));
});
fs.writeFileSync(output, JSON.stringify(rows, null, 2));
console.log(`${rows.length} rows`);
