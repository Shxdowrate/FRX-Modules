# [FRX Lab] Модуль собран в FRX WebIDE Lab. Проект лежит в конце файла — открывайте этот модуль в Lab, чтобы его менять.
import decorators
import engine
import db
import visual
import html as _html

NAME = "Простой счетчик"
MODULE_NAME = "counter_module"
VERSION = "1.2"
AUTHOR = "frx_text | FRX WebIDE Lab"
DESCRIPTION = "Простой инлайн счетчик"


# ---------- служебные функции Lab ----------

def _s(v):
    """Значение → текст (None → пусто, 3.0 → 3, True → да)."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "да" if v else "нет"
    if isinstance(v, float) and v == int(v) and abs(v) < 1e15:
        return str(int(v))
    if isinstance(v, (list, tuple)):
        return ", ".join(_s(x) for x in v)
    return str(v)

def _h(v):
    """Значение → безопасный для HTML текст."""
    return _html.escape(_s(v), quote=False)

def _n(v):
    """Значение → число (текст вида «5» и «2,5» тоже подойдёт)."""
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (int, float)):
        return v
    if v is None or v == "":
        return 0
    try:
        t = str(v).strip().replace(",", ".")
        f = float(t)
        return int(f) if f == int(f) and "." not in t and "e" not in t.lower() else f
    except (ValueError, OverflowError):
        raise ValueError(f"«{v}» — не число")


@decorators.command("counter")
async def counter_cmd(event):
    """открыть инлайн счетчик"""
    v_cc = v_form = v_text = None
    v_cc = await db.get("counter", 0)
    # (выключено) Задать
    v_text = f"{_s(visual.visual('status_on'))} Счетчик\n> {_s(v_cc)}"
    v_form = await engine.create_inlineform(chat_id=event.chat_id, text=f"{_h(v_text)}", buttons=[[("+1", "plus")], [("-1", "minus")], [("Сброс", "reset_counter")]])
    @v_form.on_click(data="plus")
    async def _on_form_plus_1(click):
        nonlocal v_cc, v_text
        v_cc = _n(v_cc) + 1
        v_text = f"{_s(visual.visual('status_on'))} Счетчик\n> {_s(v_cc)}"
        await db.set("counter", v_cc)
        await v_form.edit(f"{_h(v_text)}", buttons=v_form._raw_buttons)
    @v_form.on_click(data="minus")
    async def _on_form_minus_2(click):
        nonlocal v_cc, v_text
        v_cc = _n(v_cc) - 1
        v_text = f"{_s(visual.visual('status_on'))} Счетчик\n> {_s(v_cc)}"
        await db.set("counter", v_cc)
        await v_form.edit(f"{_h(v_text)}", buttons=v_form._raw_buttons)
    @v_form.on_click(data="reset_counter")
    async def _on_form_reset_counter_3(click):
        nonlocal v_cc, v_text
        await db.set("counter", 0)
        v_cc = 0
        v_text = f"{_s(visual.visual('status_on'))} Счетчик\n> {_s(v_cc)}"
        await v_form.edit(f"{_h(v_text)}", buttons=v_form._raw_buttons)


# ===== FRX WebIDE Lab: проект (не редактируйте вручную) =====
# FRXLAB1 104208c7d8535c
# eyJ2IjoxLCJtZXRhIjp7Im5hbWUiOiLQn9GA0L7RgdGC0L7QuSDRgdGH0LXRgtGH0LjQuiIsIm1vZHVsZV9uYW1lIjoiY291bnRl
# cl9tb2R1bGUiLCJ2ZXJzaW9uIjoiMS4yIiwiZGVzY3JpcHRpb24iOiLQn9GA0L7RgdGC0L7QuSDQuNC90LvQsNC50L0g0YHRh9C1
# 0YLRh9C40LoiLCJhdXRob3JfZXh0cmEiOiJmcnhfdGV4dCIsImxvY2tlZF9hdXRob3IiOiJmcnhfdGV4dCB8IEZSWCBXZWJJREUg
# TGFiIn0sImNvbmZpZ3MiOltdLCJzY3JpcHRzIjpbeyJpZCI6ImI0cnk2IiwidHlwZSI6ImNvbW1hbmQiLCJzaWRlIjoidXNlciIs
# InAiOnsibmFtZXMiOiJjb3VudGVyIiwiZGVzYyI6ItC+0YLQutGA0YvRgtGMINC40L3Qu9Cw0LnQvSDRgdGH0LXRgtGH0LjQuiIs
# ImFjY2VzcyI6InNlbGYifSwiYmxvY2tzIjpbeyJpZCI6ImJoY2djIiwidCI6ImRiX2dldCIsImYiOnsia2V5IjoiY291bnRlciIs
# ImRlZiI6IjAiLCJyZXMiOiJjYyIsInNjb3BlIjoibW9kdWxlIn19LHsiaWQiOiJiNm1yNSIsInQiOiJzZXQiLCJmIjp7InZhciI6
# ImNjIiwidmFsdWUiOiIwIn0sIm9mZiI6dHJ1ZX0seyJpZCI6ImI1djduIiwidCI6InNldF90ZXh0IiwiZiI6eyJ2YXIiOiJ0ZXh0
# IiwidmFsdWUiOiJ7QHZpc3VhbDpzdGF0dXNfb259INCh0YfQtdGC0YfQuNC6XG4+IHtjY30ifX0seyJpZCI6ImI3MGgwIiwidCI6
# ImZvcm1fbmV3IiwiZiI6eyJ2YXIiOiJmb3JtIiwidGV4dCI6Int0ZXh0fSIsImJ1dHRvbnMiOltbeyJsYWJlbCI6IisxIiwiZGF0
# YSI6InBsdXMiLCJraW5kIjoiY2xpY2sifV0sW3sibGFiZWwiOiItMSIsImRhdGEiOiJtaW51cyIsImtpbmQiOiJjbGljayJ9XSxb
# eyJsYWJlbCI6ItCh0LHRgNC+0YEiLCJkYXRhIjoicmVzZXRfY291bnRlciIsImtpbmQiOiJjbGljayJ9XV0sIndobyI6Im1lIiwi
# dGltZW91dCI6IjAifX0seyJpZCI6ImI5eGVwIiwidCI6Im9uX2NsaWNrIiwiZiI6eyJmb3JtIjoiZm9ybSIsImRhdGEiOiJwbHVz
# In0sImtpZHMiOnsiYm9keSI6W3siaWQiOiJiYTd2MSIsInQiOiJpbmMiLCJmIjp7InZhciI6ImNjIn19LHsiaWQiOiJiZnhjcSIs
# InQiOiJzZXRfdGV4dCIsImYiOnsidmFyIjoidGV4dCIsInZhbHVlIjoie0B2aXN1YWw6c3RhdHVzX29ufSDQodGH0LXRgtGH0LjQ
# ulxuPiB7Y2N9In19LHsiaWQiOiJiam13cyIsInQiOiJkYl9zZXQiLCJmIjp7ImtleSI6ImNvdW50ZXIiLCJ2YWx1ZSI6IntjY30i
# LCJzY29wZSI6Im1vZHVsZSJ9fSx7ImlkIjoiYmJhdnoiLCJ0IjoiZm9ybV9lZGl0IiwiZiI6eyJmb3JtIjoiZm9ybSIsInRleHQi
# OiJ7dGV4dH0iLCJtb2RlIjoia2VlcCIsImJ1dHRvbnMiOltbeyJsYWJlbCI6ItCe0LoiLCJkYXRhIjoib2siLCJraW5kIjoiY2xp
# Y2sifV1dfX1dfX0seyJpZCI6ImJjdWg0IiwidCI6Im9uX2NsaWNrIiwiZiI6eyJmb3JtIjoiZm9ybSIsImRhdGEiOiJtaW51cyJ9
# LCJraWRzIjp7ImJvZHkiOlt7ImlkIjoiYmRjMnEiLCJ0IjoiZGVjIiwiZiI6eyJ2YXIiOiJjYyJ9fSx7ImlkIjoiYmd3dWIiLCJ0
# Ijoic2V0X3RleHQiLCJmIjp7InZhciI6InRleHQiLCJ2YWx1ZSI6IntAdmlzdWFsOnN0YXR1c19vbn0g0KHRh9C10YLRh9C40Lpc
# bj4ge2NjfSJ9fSx7ImlkIjoiYmtneHEiLCJ0IjoiZGJfc2V0IiwiZiI6eyJrZXkiOiJjb3VudGVyIiwidmFsdWUiOiJ7Y2N9Iiwi
# c2NvcGUiOiJtb2R1bGUifX0seyJpZCI6ImJlMHYzIiwidCI6ImZvcm1fZWRpdCIsImYiOnsiZm9ybSI6ImZvcm0iLCJ0ZXh0Ijoi
# e3RleHR9IiwibW9kZSI6ImtlZXAiLCJidXR0b25zIjpbW3sibGFiZWwiOiLQntC6IiwiZGF0YSI6Im9rIiwia2luZCI6ImNsaWNr
# In1dXX19XX19LHsiaWQiOiJibGt4MiIsInQiOiJvbl9jbGljayIsImYiOnsiZm9ybSI6ImZvcm0iLCJkYXRhIjoicmVzZXRfY291
# bnRlciJ9LCJraWRzIjp7ImJvZHkiOlt7ImlkIjoiYm1xM2QiLCJ0IjoiZGJfc2V0IiwiZiI6eyJrZXkiOiJjb3VudGVyIiwidmFs
# dWUiOiIwIiwic2NvcGUiOiJtb2R1bGUifX0seyJpZCI6ImJvbGNzIiwidCI6InNldCIsImYiOnsidmFyIjoiY2MiLCJ2YWx1ZSI6
# IjAifX0seyJpZCI6ImJwZ3N5IiwidCI6InNldF90ZXh0IiwiZiI6eyJ2YXIiOiJ0ZXh0IiwidmFsdWUiOiJ7QHZpc3VhbDpzdGF0
# dXNfb259INCh0YfQtdGC0YfQuNC6XG4+IHtjY30ifX0seyJpZCI6ImJubnloIiwidCI6ImZvcm1fZWRpdCIsImYiOnsiZm9ybSI6
# ImZvcm0iLCJ0ZXh0Ijoie3RleHR9IiwibW9kZSI6ImtlZXAiLCJidXR0b25zIjpbW3sibGFiZWwiOiLQntC6IiwiZGF0YSI6Im9r
# Iiwia2luZCI6ImNsaWNrIn1dXX19XX19XSwiY29sbGFwc2VkIjpmYWxzZX1dfQ==
# ===== конец проекта FRX WebIDE Lab =====
