# Домашнее задание 4

Автор: Гузаиров Тимур Булатович.

- `Notification_Platform.pdf` — готовая работа.
- `Notification_Platform.docx` — редактируемая версия.
- `SOLUTION.md` — исходный текст.
- `diagrams/` — шесть диаграмм в PlantUML и PNG.
- `notification_demo.py` — прототип доставки.
- `test_notification_demo.py` и `fixtures/` — тесты и тестовые данные.

Для запуска нужен Python 3.10 или новее. Сторонние библиотеки не требуются.

```bash
python3 -m unittest -v
python3 notification_demo.py
```

Прототип работает локально с SQLite и имитацией ответов провайдеров. Реальные push, SMS и email не отправляются.
