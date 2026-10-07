TITAN HISTORY - GITHUB ACTIONS ONE CLICK

هدف:
هر بار فقط Run workflow را بزنید -> یک موضوع استفاده‌نشده انتخاب می‌شود -> منبع و تصاویر واقعی بررسی می‌شوند -> صدای محلی ساخته می‌شود -> ویدیو Short ساخته می‌شود -> روی YouTube آپلود می‌شود -> تاریخچه ضدتکرار ذخیره می‌شود.

هیچ کلید OpenRouter/Pexels/Semantic Scholar لازم نیست.
مدل Qwen به صورت محلی روی GitHub runner اجرا می‌شود و اگر نصب/دانلودش شکست بخورد، سیستم به اسکریپت evidence-only برمی‌گردد.
Piper محلی است و اگر آماده نشود، espeak-ng fallback است.
تصاویر از Wikimedia Commons هستند و attribution در Description نوشته می‌شود.

SECRET مهم:
فقط YOUTUBE_TOKEN_JSON باید در GitHub Settings > Secrets and variables > Actions > New repository secret ثبت شود.
مقدار Secret = کل متن فایل config/youtube_token.json که با AUTH_LOCAL_WINDOWS.bat ساخته می‌شود.

هرگز این‌ها را در Repository عمومی آپلود نکنید:
config/client_secret.json
config/youtube_token.json

نکته OAuth:
اگر Google OAuth app روی Testing باشد، refresh token ممکن است بعد از دوره تست منقضی شود. در آن صورت AUTH_LOCAL_WINDOWS.bat را دوباره اجرا و Secret را جایگزین کنید. برای اجرای بلندمدت بدون این انقضای Testing باید OAuth را Production کنید.
