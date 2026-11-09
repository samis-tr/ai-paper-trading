# نظام التداول الورقي — VWCE (المرحلة B: نموذج محلي)

> تداول ورقي فقط. لا يوجد في المشروع أي كود أو بيانات اعتماد لوسيط حقيقي، وهناك اختبار يفشل إذا أُضيف أي منها. لا يوجد أي ضمان للربح.

## الهيكل
| المسار | الوظيفة |
|---|---|
| `config/instruments.json` | سجل الأدوات حسب ISIN. رمز المزوّد لا يُستخدم قبل التحقق منه |
| `paper_trader/data.py` | تحميل البيانات وفحصها: متقادمة، ناقصة، غير متسقة، أو بخلل في التقويم ← توقف |
| `paper_trader/calendar_xetra.py` | تقويم XETRA |
| `paper_trader/strategy.py` | استراتيجية SMA200 حتمية بمعاملات ثابتة |
| `paper_trader/engine.py` | **المكوّن الوحيد** الذي ينشئ الأوامر الورقية وينفذها (عند افتتاح الجلسة التالية) |
| `paper_trader/ledger.py` | دفتر SQLite: إلحاقي فقط + سلسلة hash + حالة النظام (نشط / معطّل / متوقف) |
| `paper_trader/jobs.py` | الواجهة **الوحيدة** المسموحة لـ OpenClaw: 4 أوامر ثابتة (منها الإيقاف الطارئ فقط وليس الاستئناف) |
| `paper_trader/backtest.py` | اختبار رجعي بنفس المحرك + مقارنات + نوافذ walk-forward + فترة hold-out |
| `paper_trader/dashboard.py` | لوحة عربية للجوال + زر إيقاف طارئ |
| `paper_trader/notifier.py` | ملخص Telegram (أو ملف outbox بدون مفاتيح) |

## الأوامر (من داخل المجلد)
```
.venv\Scripts\python.exe -m pytest -q                          # 40 اختباراً
.venv\Scripts\python.exe -m paper_trader.cli status
.venv\Scripts\python.exe -m paper_trader.cli verify-provider   # بعد وضع EODHD_API_TOKEN في .env
.venv\Scripts\python.exe -m paper_trader.cli fetch             # يرفض الرمز غير المُتحقق منه
.venv\Scripts\python.exe -m paper_trader.cli backtest          # على البيانات الحقيقية المخزنة
.venv\Scripts\python.exe -m paper_trader.cli run-daily         # دورة يومية واحدة
.venv\Scripts\python.exe -m paper_trader.cli summary [--send]
.venv\Scripts\python.exe -m paper_trader.cli halt --who owner  # إيقاف طارئ
.venv\Scripts\python.exe -m paper_trader.cli resume --who owner  # يتطلب كتابة عبارة الموافقة
.venv\Scripts\python.exe -m paper_trader.cli add-cost 2026-11-30 Hetzner 4.99 --note "invoice #"
.venv\Scripts\python.exe -m paper_trader.cli job run_daily     # ما يستدعيه OpenClaw
```
اللوحة: `.venv\Scripts\python.exe -m uvicorn paper_trader.dashboard:app --host 127.0.0.1 --port 8765`

## ملاحظات تصميمية مهمة
- **الحدود ليست سقفاً مضموناً**: في العرض التجريبي توقف النظام عند تراجع 3.13% وليس 3.00%، بسبب فجوة سعرية. بعد التوقف يبقى المركز مفتوحاً ويستمر التقييم فقط (وصل التراجع إلى 4.6%).
- **الاستئناف** بعد التعطيل أو الإيقاف يدوي فقط، بعبارة موافقة يكتبها المالك. لا يملك أي وكيل هذه القدرة.
- **البيانات الاصطناعية** (`SYNTHETIC-TEST-ONLY`) للاختبار فقط، ويرفضها `run-daily`.
