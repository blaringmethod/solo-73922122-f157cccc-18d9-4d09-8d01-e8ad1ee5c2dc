"""Flask 网页层：交易日历、换月计划、报价补录/更正、连续价取数与 CSV 下载。"""

from __future__ import annotations

import io
import os

from flask import (
    Flask, Response, flash, g, jsonify, redirect, render_template,
    request, url_for,
)

from . import db, demo


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.environ.get("CONT_SECRET_KEY", "dev-secret-change-me")

    # 启动即建表（IF NOT EXISTS，幂等），保证容器起来即可用；
    # 同时为旧库首次补建回落线段树索引（新库为空索引，开销常数级）
    boot = db.connect()
    try:
        db.init_db(boot)
        db.ensure_drawdown_index(boot)
    finally:
        boot.close()

    def get_conn():
        if "conn" not in g:
            g.conn = db.connect()
        return g.conn

    @app.teardown_appcontext
    def close_conn(exc):
        conn = g.pop("conn", None)
        if conn is not None:
            conn.close()

    # ------------------------------------------------------------------ #
    # 首页 / 总览
    # ------------------------------------------------------------------ #
    @app.route("/")
    def index():
        conn = get_conn()
        return render_template(
            "index.html",
            overview=db.overview(conn),
            contracts=db.list_contracts(conn),
            rolls=db.list_rolls(conn),
        )

    # ------------------------------------------------------------------ #
    # 交易日历
    # ------------------------------------------------------------------ #
    @app.route("/calendar", methods=["GET", "POST"])
    def calendar_view():
        conn = get_conn()
        if request.method == "POST":
            raw = request.form.get("dates", "")
            dates = [line.strip() for line in raw.replace(",", "\n").splitlines()]
            try:
                n = db.replace_calendar(conn, dates)
                flash(f"交易日历已保存，共 {n} 个交易日", "ok")
            except db.AppError as e:
                flash(str(e), "err")
            return redirect(url_for("calendar_view"))
        return render_template("calendar.html", dates="\n".join(db.list_calendar(conn)))

    # ------------------------------------------------------------------ #
    # 换月计划
    # ------------------------------------------------------------------ #
    @app.route("/plan", methods=["GET", "POST"])
    def plan_view():
        conn = get_conn()
        if request.method == "POST":
            initial = request.form.get("initial_code", "")
            switch_dates = request.form.getlist("switch_date")
            switch_codes = request.form.getlist("switch_code")
            switches = list(zip(switch_dates, switch_codes))
            try:
                db.setup_plan(conn, initial, switches)
                flash("初始合约与换月计划已登记", "ok")
            except db.AppError as e:
                flash(str(e), "err")
            return redirect(url_for("plan_view"))
        return render_template(
            "plan.html",
            contracts=db.list_contracts(conn),
            rolls=db.list_rolls(conn),
        )

    # ------------------------------------------------------------------ #
    # 报价：补录 / 更正
    # ------------------------------------------------------------------ #
    @app.route("/prices", methods=["GET", "POST"])
    def prices_view():
        conn = get_conn()
        if request.method == "POST":
            try:
                contract_id = int(request.form["contract_id"])
                day = request.form["date"].strip()
                price = int(request.form["price"])
                # 先在当前状态上做一次“预览计算”；即使后续保存失败，结果仍可展示
                preview = db.upsert_price(conn, contract_id, day, price)
                if not preview["committed"]:
                    flash(f"保存失败已回滚（{preview.get('error', '未知错误')}），"
                          f"预期结果：{preview['contract']} {day} = {preview['price']} 分",
                          "err")
                else:
                    if preview["is_correction"]:
                        msg = (f"已更正 {preview['contract']} {day} 报价："
                               f"{preview['old_price']} → {preview['price']} 分")
                    else:
                        msg = f"已补录 {preview['contract']} {day} 报价 {preview['price']} 分"
                    if preview.get("affected_roll_ord") is not None:
                        d = preview.get("new_delta")
                        d_txt = "仍缺报价" if d is None else f"新价差 = {d} 分"
                        msg += f"；联动重算第 {preview['affected_roll_ord']} 次换月（{d_txt}）"
                    flash(msg, "ok")
            except db.AppError as e:
                flash(str(e), "err")
            except (ValueError, KeyError):
                flash("合约、日期或价格输入不合法（价格须为整数分）", "err")
            return redirect(url_for("prices_view"))

        sel = request.args.get("contract_id", type=int)
        return render_template(
            "prices.html",
            contracts=db.list_contracts(conn),
            prices=db.list_prices(conn, sel),
            selected=sel,
        )

    # ------------------------------------------------------------------ #
    # 取数：连续价查询 + CSV 下载 + 区间最大回落
    # ------------------------------------------------------------------ #
    @app.route("/query", methods=["GET"])
    def query_view():
        conn = get_conn()
        start = request.args.get("start", "").strip()
        end = request.args.get("end", "").strip()
        base = request.args.get("base", "").strip()
        rows = None
        err = None
        if start or end or base:
            try:
                rows = db.fetch_range(conn, start, end, base)
            except db.AppError as e:
                err = str(e)

        # 区间回落：优先用回落表单自己的起止日；否则复用连续价表单的起止日
        raw_dd_start = request.args.get("dd_start", "").strip()
        raw_dd_end = request.args.get("dd_end", "").strip()
        if raw_dd_start or raw_dd_end:
            dd_start, dd_end = raw_dd_start, raw_dd_end
            dd_submitted = True
        elif start and end:
            dd_start, dd_end = start, end
            dd_submitted = True
        else:
            dd_start = dd_end = ""
            dd_submitted = False
        dd = None
        dd_err = None
        if dd_submitted:
            try:
                dd = db.find_max_drawdown(conn, dd_start, dd_end, details=True)
            except db.AppError as e:
                dd_err = str(e)

        demo_mode = request.args.get("demo", "").strip()
        return render_template(
            "query.html",
            start=start, end=end, base=base, rows=rows, err=err,
            dd_start=dd_start, dd_end=dd_end, dd=dd, dd_err=dd_err,
            dd_submitted=dd_submitted, demo_mode=demo_mode,
            demo_const=demo,
        )

    @app.route("/drawdown")
    def drawdown_alias():
        """便于直接访问/外部探活的别名：落到连续价页的回落区间。"""
        dd_start = request.args.get("dd_start") or request.args.get("start", "")
        dd_end = request.args.get("dd_end") or request.args.get("end", "")
        return redirect(url_for(
            "query_view", dd_start=dd_start, dd_end=dd_end, _anchor="drawdown"
        ))

    @app.route("/api/drawdown")
    def api_drawdown():
        """机器可读的最大回落 JSON；同一快照读取，核算只触 O(log n) 索引记录。"""
        conn = get_conn()
        start = (request.args.get("start")
                 or request.args.get("dd_start") or "").strip()
        end = (request.args.get("end")
               or request.args.get("dd_end") or "").strip()
        try:
            result = db.find_max_drawdown(conn, start, end, details=False)
        except db.AppError as e:
            return jsonify({"ok": False, "error": str(e)}), 400
        return jsonify({"ok": True, **result})

    # ------------------------------------------------------------------ #
    # 网页演示：换月 + 补价前后的回落
    # ------------------------------------------------------------------ #
    @app.post("/demo/seed")
    def demo_seed():
        conn = get_conn()
        try:
            before = demo.seed_before(conn)
            if before.get("found"):
                flash(
                    f"演示场景已载入（补价前）：一次换月 {demo.ROLL_DATE} "
                    f"{demo.INITIAL_CODE} → {demo.NEW_CODE}，且旧合约在换月参考日 "
                    f"2026-03-04 缺价。当前最大回落 {before['amount']} 分"
                    f"（{before['start']} → {before['end']}，正是换月边）。"
                    f"再点「补入关键报价」看变化。",
                    "ok",
                )
            else:
                flash("演示场景已载入（补价前）。", "ok")
        except Exception as e:  # 演示按钮不应抛出裸 500
            app.logger.exception("demo seed failed")
            flash(f"演示场景载入失败：{e}", "err")
        return redirect(url_for(
            "query_view",
            dd_start=demo.DEMO_START, dd_end=demo.DEMO_END,
            demo="before", _anchor="drawdown",
        ))

    @app.post("/demo/fill")
    def demo_fill():
        conn = get_conn()
        try:
            before, after = demo.fill_key_quote(conn)
            if before.get("found") and after.get("found"):
                flash(
                    f"补价已保存并与换月价差、回落索引在同一事务生效。"
                    f"补价前最大回落 {before['amount']} 分"
                    f"（{before['start']} → {before['end']}）；"
                    f"补价后最大回落 {after['amount']} 分"
                    f"（{after['start']} → {after['end']}）。",
                    "ok",
                )
            else:
                flash("补价已保存。", "ok")
        except Exception as e:
            app.logger.exception("demo fill failed")
            flash(f"补价失败已回滚，照旧供数：{e}", "err")
        return redirect(url_for(
            "query_view",
            dd_start=demo.DEMO_START, dd_end=demo.DEMO_END,
            demo="after", _anchor="drawdown",
        ))

    @app.post("/demo/unfill")
    def demo_unfill():
        conn = get_conn()
        try:
            demo.unfill_key_quote(conn)
            flash("已删除关键报价，回到补价前状态，可重新演示补价过程。", "ok")
        except Exception as e:
            app.logger.exception("demo unfill failed")
            flash(f"删除失败：{e}", "err")
        return redirect(url_for(
            "query_view",
            dd_start=demo.DEMO_START, dd_end=demo.DEMO_END,
            demo="before", _anchor="drawdown",
        ))

    @app.route("/download")
    def download():
        conn = get_conn()
        start = request.args.get("start", "").strip()
        end = request.args.get("end", "").strip()
        base = request.args.get("base", "").strip()
        if not start or not end or not base:
            flash("下载前请先填写开始日、结束日和基准日", "err")
            return redirect(url_for("query_view"))
        try:
            rows = db.fetch_range(conn, start, end, base)
        except db.AppError as e:
            flash(str(e), "err")
            return redirect(url_for("query_view"))
        csv_text = db.to_csv(rows)
        fname = f"continuous_{start}_{end}.csv"
        return Response(
            "﻿" + csv_text,  # UTF-8 BOM，Excel 直接打开不乱码
            mimetype="text/csv; charset=utf-8",
            headers={"Content-Disposition": f"attachment; filename={fname}"},
        )

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
