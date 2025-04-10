import gradio as gr
from collections import defaultdict
import pandas as pd
import csv
import os
from datetime import date
import requests

# 全局变量，用于记录出行人员和支出条目
participants = []  # 出行人员列表，每个元素为字符串姓名
entries = []  # 支出条目列表，每个条目为字典

CSV_FILENAME = "trip_expenses.csv"


def get_exchange_rates():
    """
    获取以英镑(GBP)为基准的实时汇率。
    支持的币种：
        - "英镑": 对应 "GBP"（汇率1.0）
        - "欧元": 对应 "EUR"
        - "人民币": 对应 "CNY"
    计算方法：对于非英镑币种，转换系数 = 1 / API返回的该币种汇率，
    即：支出币种金额 * (1 / rate) = 对应的英镑金额。
    如果调用失败，则使用预设的默认汇率。
    """
    # 默认汇率：单位为 英镑/单位货币（大致值，仅供参考）
    default_rates = {"英镑": 1.0, "欧元": 0.86, "人民币": 0.11}
    try:
        # 由于 CSV 中币种为中文，这里建立对应关系
        symbol_map = {"英镑": "GBP", "欧元": "EUR", "人民币": "CNY"}
        symbols = ",".join([symbol_map[c] for c in symbol_map])
        url = f"https://api.exchangerate.host/latest?base=GBP&symbols={symbols}"
        response = requests.get(url, timeout=5)
        data = response.json()
        rates = {}
        # 对于英镑，本身转换系数为1
        rates["英镑"] = 1.0
        # 对于其他币种，换算为英镑：金额_in_GBP = amount / (API_rate)
        for cn, symbol in symbol_map.items():
            if cn == "英镑":
                continue
            if symbol in data.get("rates", {}):
                # 1 GBP = data["rates"][symbol] 单位 X，因此 1 单位X = 1 / data["rates"][symbol] GBP
                rates[cn] = 1 / data["rates"][symbol]
            else:
                rates[cn] = default_rates[cn]
        return rates
    except Exception as e:
        return default_rates


def compute_result():
    """
    根据全局变量 participants 与 entries 计算平摊矩阵，更新内容包括：
      1. 将不同币种的支出转换为英镑，汇总成一个统一的英镑平账矩阵。
      2. 通过“平账矩阵 - 转置矩阵”的方式直接得到净额矩阵，
         从而减少转账次数（例如，alice给bob转120镑，而bob给alice转100镑，最后只需alice转20镑给bob）。

    最后返回 HTML 格式的结果展示，其中包括：
      - 整合后的英镑平账矩阵。
      - 根据净额矩阵得到的转账方案（只显示净额大于0的部分）。
    """
    # 按币种构建转账记录，每条记录：转账人 debtor 给 收款人 creditor
    transfer = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))
    all_people = set(participants)
    for entry in entries:
        currency = entry["currency"]
        amount = entry["amount"]
        payer = entry["payer"]
        share_list = entry["share"]
        n = len(share_list)
        if n == 0:
            continue
        share_amt = amount / n
        for person in share_list:
            all_people.add(person)
            if person != payer:
                transfer[currency][person][payer] += share_amt

    people_list = sorted(all_people)
    # 获取实时汇率，将各币种转为英镑金额
    conversion_rates = get_exchange_rates()
    net_transfer = defaultdict(lambda: defaultdict(float))
    for curr, matrix in transfer.items():
        rate = conversion_rates.get(curr, 1.0)
        for debtor, subdict in matrix.items():
            for creditor, amt in subdict.items():
                net_transfer[debtor][creditor] += amt * rate

    # 采用“平账矩阵 - 转置矩阵”直接计算净额矩阵（减少转账次数）
    # 对于任意一对 (i, j)，净额 = net_transfer[i][j] - net_transfer[j][i]
    reduced_matrix = defaultdict(lambda: defaultdict(float))
    transactions = []  # 存放净转账方案： (付款人, 收款人, 金额)
    for debtor in people_list:
        for creditor in people_list:
            if debtor == creditor:
                continue
            net_amt = net_transfer[debtor].get(creditor, 0.0) - net_transfer[creditor].get(debtor, 0.0)
            if net_amt > 1e-9:
                reduced_matrix[debtor][creditor] = net_amt
                transactions.append((debtor, creditor, net_amt))
            else:
                reduced_matrix[debtor][creditor] = 0.0

    # 构造整合后的英镑平账矩阵显示
    html_result = "<h2>整合后的英镑平账矩阵（已抵消互相转账）</h2>"
    html_result += "<table border='1' style='border-collapse: collapse;'>"
    html_result += "<tr><th>转账人 \\ 收款人</th>"
    for person in people_list:
        html_result += f"<th>{person}</th>"
    html_result += "</tr>"
    for debtor in people_list:
        html_result += f"<tr><td>{debtor}</td>"
        for creditor in people_list:
            if debtor == creditor:
                html_result += "<td>-</td>"
            else:
                amt = reduced_matrix[debtor][creditor]
                html_result += f"<td>{amt:.2f}</td>"
        html_result += "</tr>"
    html_result += "</table>"

    # 构造转账方案的显示
    html_result += "<h2>转账方案</h2>"
    if transactions:
        html_result += "<ul>"
        for debtor, creditor, amt in transactions:
            html_result += f"<li>{debtor} -> {creditor}: {amt:.2f} 英镑</li>"
        html_result += "</ul>"
    else:
        html_result += "<p>无需转账，大家已经平账！</p>"

    # 如果无有效数据，则给出提示
    if not transactions and not any(sum(v.values()) > 1e-9 for v in reduced_matrix.values()):
        html_result = "<p>没有有效数据，请检查输入的支出条目。</p>"
    return html_result


def save_to_csv(rows):
    """
    将传入的支出条目 rows 写入 CSV 文件中。
    格式为：账单日期, Reference, 支出者, 货币种类, 金额, 然后每个承担者单独一列。
    文件采用覆盖写入，每次提交时覆盖原有数据。
    """
    with open(CSV_FILENAME, mode="w", newline="", encoding="utf-8") as csvfile:
        writer = csv.writer(csvfile)
        header = ["账单日期", "Reference", "支出者", "货币种类", "金额", "承担者们"]
        writer.writerow(header)
        for row in rows:
            if len(row) == 6:
                consumers = [s.strip() for s in row[5].split(",") if s.strip()]
            else:
                consumers = [str(s).strip() for s in row[5:] if str(s).strip()]
            out_row = [row[0], row[1], row[2], row[3], row[4]] + consumers
            writer.writerow(out_row)


with gr.Blocks() as demo:
    gr.Markdown("## 添加出行人员")
    with gr.Row():
        participant_input = gr.Textbox(label="出行人员姓名", placeholder="输入姓名后点击添加")
        add_participant_button = gr.Button("添加出行人员")
    participant_list = gr.Dataframe(value=[], headers=["出行人员"], interactive=False, label="已添加出行人员")

    gr.Markdown("## 添加支出条目")
    with gr.Row():
        bill_date_input = gr.Textbox(label="账单日期", value=str(date.today()))
        reference_input = gr.Textbox(label="Reference (消费内容)", placeholder="例如：餐饮/酒店/出行等")
        currency_input = gr.Dropdown(label="货币种类", choices=["欧元", "人民币", "英镑"], value="英镑")
        amount_input = gr.Number(label="金额", value=0)
    with gr.Row():
        payer_input = gr.Dropdown(label="支出者", choices=[], value=None)
        share_selector = gr.CheckboxGroup(label="承担者", choices=[])
    add_entry_button = gr.Button("添加支出条目")
    entry_list = gr.Dataframe(value=[], headers=["账单日期", "Reference", "支出者", "货币种类", "金额", "承担者"],
                              interactive=False,
                              label="已添加支出条目")

    with gr.Row():
        delete_index_input = gr.Textbox(label="删除条目的行号", placeholder="请输入要删除的行号（从1开始）")
        delete_entry_button = gr.Button("删除支付条目")

    gr.Markdown("## 读取CSV文件并添加到支出条目")
    load_csv_button = gr.Button("读取CSV文件")

    gr.Markdown("## 计算平摊结果并保存到 CSV")
    compute_button = gr.Button("Submit")
    result_output = gr.HTML(label="分账结果展示")


    def update_participants(name, current_df):
        if current_df is None or (isinstance(current_df, list) and len(current_df) == 0) or (
                isinstance(current_df, pd.DataFrame) and current_df.empty):
            current_list = []
        else:
            if isinstance(current_df, pd.DataFrame):
                current_list = current_df.iloc[:, 0].tolist()
            else:
                current_list = [row[0] for row in current_df]
        if name and name not in current_list:
            current_list.append(name)
        output = [[p] for p in current_list]
        return output, output


    add_participant_button.click(
        fn=update_participants,
        inputs=[participant_input, participant_list],
        outputs=[participant_list, participant_list]
    )


    def update_entries(bill_date, reference, currency, amount, payer, share, current_df):
        if current_df is None or (isinstance(current_df, list) and len(current_df) == 0) or (
                isinstance(current_df, pd.DataFrame) and current_df.empty):
            current_entries = []
        else:
            if isinstance(current_df, pd.DataFrame):
                current_entries = current_df.values.tolist()
            else:
                current_entries = [list(row) for row in current_df]
        try:
            amount = float(amount)
        except:
            return current_entries, current_entries
        share_display = ",".join(share) if share else ""
        new_entry = [bill_date, reference, payer, currency, amount, share_display]
        current_entries.append(new_entry)
        return current_entries, current_entries


    add_entry_button.click(
        fn=update_entries,
        inputs=[bill_date_input, reference_input, currency_input, amount_input, payer_input, share_selector,
                entry_list],
        outputs=[entry_list, entry_list]
    )


    def delete_entry(delete_index, current_df):
        if current_df is None or (isinstance(current_df, pd.DataFrame) and current_df.empty):
            return current_df, current_df
        if isinstance(current_df, pd.DataFrame):
            rows = current_df.values.tolist()
        else:
            rows = [list(row) for row in current_df]
        try:
            idx = int(delete_index) - 1
        except Exception:
            return current_df, current_df
        if idx < 0 or idx >= len(rows):
            return current_df, current_df
        del rows[idx]
        return rows, rows


    delete_entry_button.click(
        fn=delete_entry,
        inputs=[delete_index_input, entry_list],
        outputs=[entry_list, entry_list]
    )


    def update_dropdown(current_df):
        if current_df is None or (isinstance(current_df, list) and len(current_df) == 0) or (
                isinstance(current_df, pd.DataFrame) and current_df.empty):
            return gr.update(choices=[], value=None)
        if isinstance(current_df, pd.DataFrame):
            new_choices = current_df.iloc[:, 0].tolist()
        else:
            new_choices = [row[0] for row in current_df]
        return gr.update(choices=new_choices, value=None)


    participant_list.change(fn=update_dropdown, inputs=[participant_list], outputs=[payer_input])
    participant_list.change(fn=update_dropdown, inputs=[participant_list], outputs=[share_selector])


    def load_csv_to_entries(current_entries):
        loaded_rows = []
        if os.path.exists(CSV_FILENAME):
            with open(CSV_FILENAME, mode="r", newline="", encoding="utf-8") as csvfile:
                reader = csv.reader(csvfile)
                headers = next(reader, None)
                for row in reader:
                    if len(row) > 6:
                        row[5] = ",".join(row[5:])
                        row = row[:6]
                    loaded_rows.append(row)
        if current_entries is None or (isinstance(current_entries, pd.DataFrame) and current_entries.empty):
            current_list = []
        else:
            if isinstance(current_entries, pd.DataFrame):
                current_list = current_entries.values.tolist()
            else:
                current_list = [list(row) for row in current_entries]
        combined = current_list + loaded_rows
        return combined


    load_csv_button.click(
        fn=load_csv_to_entries,
        inputs=[entry_list],
        outputs=[entry_list]
    )


    def on_submit(current_participants, current_entries):
        global participants, entries
        if current_participants is None or (
                isinstance(current_participants, pd.DataFrame) and current_participants.empty) or (
                isinstance(current_participants, list) and len(current_participants) == 0):
            participants_list = []
        else:
            if isinstance(current_participants, pd.DataFrame):
                participants_list = current_participants.iloc[:, 0].tolist()
            else:
                participants_list = [row[0] for row in current_participants]
        participants[:] = participants_list

        entries.clear()
        rows_to_save = []
        if current_entries is not None:
            if isinstance(current_entries, pd.DataFrame):
                if not current_entries.empty:
                    current_entries = current_entries.values.tolist()
                else:
                    current_entries = []
            else:
                current_entries = [list(row) for row in current_entries]
            for row in current_entries:
                if len(row) < 5:
                    continue
                bill_date = row[0]
                reference = row[1]
                payer = row[2]
                currency = row[3]
                try:
                    amount = float(row[4])
                except:
                    continue
                if len(row) > 6:
                    share_list = [str(p).strip() for p in row[5:] if str(p).strip()]
                else:
                    share_list = [p.strip() for p in row[5].split(",") if p.strip()]
                entries.append({
                    "bill_date": bill_date,
                    "reference": reference,
                    "payer": payer,
                    "currency": currency,
                    "amount": amount,
                    "share": share_list
                })
                rows_to_save.append(row)
        if rows_to_save:
            save_to_csv(rows_to_save)
        return compute_result()


    compute_button.click(
        fn=on_submit,
        inputs=[participant_list, entry_list],
        outputs=result_output
    )

if __name__ == "__main__":
    demo.launch()
