const fs = require("fs");
const path = require("path");

const analyticsId = process.env.GOOGLE_ANALYTICS_ID || process.env.GA_MEASUREMENT_ID || "";
const indexPath = path.join(__dirname, "..", "site", "index.html");
const placeholder = "    <!-- GOOGLE_ANALYTICS_TAG -->";
const tagPattern =
  /    <!-- GOOGLE_ANALYTICS_TAG -->|    <script async src="https:\/\/www\.googletagmanager\.com\/gtag\/js\?id=[^"]+"><\/script>\n    <script>\n      window\.dataLayer = window\.dataLayer \|\| \[\];\n      function gtag\(\) {\n        dataLayer\.push\(arguments\);\n      }\n      gtag\("js", new Date\(\)\);\n      gtag\("config", "[^"]+"\);\n    <\/script>/;

let html = fs.readFileSync(indexPath, "utf8");
const trimmedAnalyticsId = analyticsId.trim();

if (!trimmedAnalyticsId) {
  html = html.replace(tagPattern, placeholder);
  fs.writeFileSync(indexPath, html);
  console.log("Google Analytics ID not set; leaving analytics disabled.");
  process.exit(0);
}

if (!/^G-[A-Z0-9]+$/.test(trimmedAnalyticsId)) {
  throw new Error(
    `Invalid Google Analytics measurement ID: ${trimmedAnalyticsId}. Expected a GA4 ID like G-XXXXXXXXXX.`
  );
}

const analyticsTag = `    <script async src="https://www.googletagmanager.com/gtag/js?id=${trimmedAnalyticsId}"></script>
    <script>
      window.dataLayer = window.dataLayer || [];
      function gtag() {
        dataLayer.push(arguments);
      }
      gtag("js", new Date());
      gtag("config", "${trimmedAnalyticsId}");
    </script>`;

if (!tagPattern.test(html)) {
  throw new Error("Could not find the Google Analytics placeholder in site/index.html.");
}

html = html.replace(tagPattern, analyticsTag);
fs.writeFileSync(indexPath, html);
console.log(`Injected Google Analytics tag for ${trimmedAnalyticsId}.`);
