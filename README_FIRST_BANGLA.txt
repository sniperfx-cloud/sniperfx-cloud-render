SniperFx Render Cloud v2 — No Coding, বাংলা Step-by-Step
=========================================================

⚠️ প্রথমে বুঝুন: Railway-এর ২১ অক্টোবর ঘটনার পর নতুন signup বন্ধ।
এটি Railway নয়—Render-এর জন্য আলাদা, পরিবর্তিত package। আগের Railway ZIP নয়।

প্রয়োজন: GitHub account, Render account, আপনার আগের Telegram Bot Token,
Twelve Data API Key, একটি NEW 6-digit pairing PIN, paid worker ও disk billing.
Keys/PIN কাউকে দেবেন না; GitHub-এও লিখবেন না।

ধাপ ১. Windows-এ পুরনো SniperFx কালো CMD window বন্ধ করুন।
ধাপ ২. GitHub.com/new -> Repository name: sniperfx-cloud-render -> Private -> Create.
ধাপ ৩. এই ZIP Extract করুন। GitHub repo-তে Add File -> Upload files দিন।
        EXTRACTED ফাইলগুলো repo ROOT-এ দিন (বিশেষ করে render.yaml, Dockerfile,
        cloud_main.py, strategy_engine.py)। ZIP নিজে আপলোড করলে কাজ করবে না।
        তারপর 'Commit changes' দিন।
ধাপ ৪. https://dashboard.render.com/ এ account তৈরি করুন বা Sign In করুন।
        New (+) -> Blueprint -> Connect আপনার PRIVATE GitHub Repository.
        render.yaml দেখালে Continue -> Deploy Blueprint।
ধাপ ৫. তিনটি secret ENV VALUE (শুধু Render-এ):
        TELEGRAM_BOT_TOKEN    = আগের BotFather token
        TWELVE_DATA_API_KEY  = Twelve Data API key
        PAIRING_PIN          = আপনার নতুন 6-digit private pin (স্ক্রিনশটের পুরনো PIN নয়)
        SNIPERFX_DATA_DIR স্বয়ংক্রিয়ভাবে /data থাকবে।
        render.yaml-এ worker=1, disk=/data, region=Singapore, Docker pre-configured।
        কম খরচের paid worker এবং 1GB storage-এর বিল দেখাবে: অনুমোদনের আগে যাচাই করুন।
        Render-এ billing / card verification চাইতে পারে।
ধাপ ৬. Deploy-এ অনুমতি দিয়ে Logs দেখুন। 'SNIPERFX CLOUD v2: ready' দেখলে
        Cloud শুরু হয়েছে, তবে Telegram এবং Market Data connectivity যাচাই বাকি।
ধাপ ৭. Telegram SniperFx chat-এ পাঠান:
        /start আপনার_নতুন_৬_সংখ্যার_PIN
        /capital 1000           <-- আপনার আসল DEMO USD Equity আলাদা হলে বদলান
        /account raw            <-- Cent হলে /account cent
        /risk 0.5
        /status
        /activate
        /status
        'Cloud: RUNNING | Alerts: ON' এবং Scanner status যাচাই করুন।
        Setup শেষেও valid signal না থাকলে কোনো Signal আসবে না।

সুরক্ষা: Demo Only. Backtested/validated profitability নেই। Auto MT5 trading নয়।
Broker price, spread, slippage, lot size, news, open exposure independent verification.
Windows-এর পুরনো bot একসঙ্গে চালাবেন না; Telegram getUpdates conflict হবে।
Cloud-এ Dashboard http://127.0.0.1:8765 নেই; Telegram /status, Render Logs দেখুন।
Paid compute always-on হলেও network/API/provider outage সম্ভব; 100% uptime নয়।
Twelve Data Free plan rate limits/asset coverage check করুন।

Render official docs: https://render.com/docs/infrastructure-as-code
Render pricing: https://render.com/pricing

Trouble? Render Service -> Logs-এর screenshot পাঠাবেন।
কোনো token/key/PIN যেন screenshot-এ দেখা না যায়।
