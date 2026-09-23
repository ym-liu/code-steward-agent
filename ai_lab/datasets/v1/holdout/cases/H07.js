async function check(url) {
  const response = await fetch(url);
  return {status: response.status, ok: response.ok};
}
module.exports = {check};
