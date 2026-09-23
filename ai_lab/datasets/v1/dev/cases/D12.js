const fs = require("node:fs");
const values = fs.readFileSync(0, "utf8").trim().split(/\s+/).map(Number);
const total = values.filter(Number.isFinite).reduce((sum, n) => sum + n, 0);
console.log(total);
