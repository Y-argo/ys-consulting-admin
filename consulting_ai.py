# consulting_ai.py
# ============================================================
# ASCEND コンサルAI拡張モジュール
# app.py と同階層に配置し、app.py から import して使う
# ============================================================
# 依存: call_llm / db / authed_uid / DEFAULT_TENANT は
#       render_consulting_ai_tab() の引数で受け取る疎結合設計
# ============================================================

from __future__ import annotations

import json
import re
import csv
import io
import datetime
import uuid
from typing import Optional, Any, Callable

import streamlit as st

# ============================================================
# 定数
# ============================================================
COL_ANALYSES   = "consulting_analyses"
COL_PLANS      = "consulting_action_plans"
COL_FRAMEWORKS = "consulting_frameworks"

ANALYSIS_TYPES = {
    "structure_diagnosis": "構造診断",
    "issue_hypothesis":    "論点・仮説設計",
    "comparison":          "比較表",
    "contradiction_check": "矛盾検出",
    "execution_plan":      "実行プラン",
}

DEFAULT_COMPARISON_AXES = [
    "収益性", "実行難易度", "初期コスト",
    "回収期間", "再現性", "リスク", "拡張性",
]

# ============================================================
# JSON 安全パーサー
# ============================================================
def _safe_parse_json(text: str) -> Any:
    """LLM出力からJSON部分を抽出してパース。失敗時は None を返す。"""
    if not text:
        return None
    # ```json ... ``` または ``` ... ``` ブロックを優先抽出
    m = re.search(r"```(?:json)?\s*([\s\S]+?)```", text, re.IGNORECASE)
    candidate = m.group(1).strip() if m else text.strip()
    # 先頭 { か [ を探して以降を切り出す
    for start_char, end_char in [('{', '}'), ('[', ']')]:
        idx = candidate.find(start_char)
        if idx == -1:
            continue
        sub = candidate[idx:]
        # 末尾の余分なテキストを削除 (最後の対応閉じ括弧を探す)
        depth = 0
        last_idx = -1
        for i, ch in enumerate(sub):
            if ch == start_char:
                depth += 1
            elif ch == end_char:
                depth -= 1
                if depth == 0:
                    last_idx = i
                    break
        if last_idx != -1:
            try:
                return json.loads(sub[:last_idx + 1])
            except Exception:
                pass
    try:
        return json.loads(candidate)
    except Exception:
        return None


# ============================================================
# Firestore CRUD
# ============================================================
def _save_analysis(db, tenant_id: str, user_id: str,
                   analysis_type: str, input_text: str,
                   result_json: dict) -> str:
    analysis_id = str(uuid.uuid4())
    now = datetime.datetime.utcnow()
    doc = {
        "analysis_id":   analysis_id,
        "tenant_id":     tenant_id,
        "user_id":       user_id or "",
        "input_text":    input_text,
        "analysis_type": analysis_type,
        "result_json":   result_json,
        "created_at":    now,
        "updated_at":    now,
    }
    db.collection(COL_ANALYSES).document(analysis_id).set(doc)
    return analysis_id


def _load_analyses(db, tenant_id: str, analysis_type: str,
                   limit: int = 20) -> list:
    try:
        snap = (
            db.collection(COL_ANALYSES)
            .where("tenant_id", "==", tenant_id)
            .where("analysis_type", "==", analysis_type)
            .stream()
        )
        docs = [s.to_dict() for s in snap]
        docs.sort(key=lambda d: str(d.get("created_at", "")), reverse=True)
        return docs[:limit]
    except Exception as e:
        st.warning(f"⚠️ 履歴の読み込みに失敗しました: {e}")
        return []


def _save_action_plan(db, tenant_id: str, analysis_id: str,
                      tasks: list) -> None:
    now = datetime.datetime.utcnow()
    for task in tasks:
        plan_id = str(uuid.uuid4())
        doc = {
            "plan_id":     plan_id,
            "analysis_id": analysis_id,
            "tenant_id":   tenant_id,
            "task":        task.get("task", ""),
            "owner":       task.get("owner", ""),
            "deadline":    task.get("deadline", ""),
            "kpi":         task.get("kpi", ""),
            "priority":    task.get("priority", "medium"),
            "status":      "open",
            "created_at":  now,
            "updated_at":  now,
        }
        db.collection(COL_PLANS).document(plan_id).set(doc)


def _load_action_plans(db, tenant_id: str, limit: int = 50) -> list:
    try:
        snap = (
            db.collection(COL_PLANS)
            .where("tenant_id", "==", tenant_id)
            .limit(limit * 3)
            .stream()
        )
        docs = [s.to_dict() for s in snap]
        docs.sort(key=lambda d: str(d.get("created_at", "")), reverse=True)
        return docs[:limit]
    except Exception:
        return []


def _update_plan_status(db, plan_id: str, status: str) -> None:
    db.collection(COL_PLANS).document(plan_id).update({
        "status": status,
        "updated_at": datetime.datetime.utcnow(),
    })


def _load_frameworks(db, tenant_id: str) -> list:
    """コンサルフレームワーク定義をDBから読み込む。なければデフォルトを返す。"""
    try:
        snap = (
            db.collection(COL_FRAMEWORKS)
            .where("tenant_id", "==", tenant_id)
            .where("active", "==", True)
            .stream()
        )
        docs = [s.to_dict() for s in snap]
        if docs:
            return docs
    except Exception:
        pass
    return _default_frameworks()


def _load_all_frameworks(db, tenant_id: str) -> list:
    """active フラグに関わらず全フレームワークを返す（管理UI用）。"""
    try:
        snap = (
            db.collection(COL_FRAMEWORKS)
            .where("tenant_id", "==", tenant_id)
            .stream()
        )
        docs = [s.to_dict() for s in snap]
        return docs if docs else _default_frameworks()
    except Exception:
        return _default_frameworks()


def _save_consulting_framework(db, tenant_id: str,
                                name: str, description: str = "",
                                active: bool = True,
                                framework_id: str = None) -> str:
    """
    フレームワークを新規作成または上書き保存する。
    framework_id を渡すと上書き、省略すると新規UUID を採番。
    戻り値: framework_id
    """
    fid = framework_id or str(uuid.uuid4())
    now = datetime.datetime.utcnow()
    doc = {
        "framework_id": fid,
        "tenant_id":    tenant_id,
        "name":         name.strip(),
        "description":  description.strip(),
        "active":       active,
        "updated_at":   now,
    }
    db.collection(COL_FRAMEWORKS).document(fid).set(doc, merge=True)
    return fid


def _toggle_framework_active(db, framework_id: str, active: bool) -> None:
    """フレームワークの active フラグを切り替える。"""
    db.collection(COL_FRAMEWORKS).document(framework_id).update({
        "active":     active,
        "updated_at": datetime.datetime.utcnow(),
    })


def _delete_consulting_framework(db, framework_id: str) -> None:
    """フレームワークを物理削除する。"""
    db.collection(COL_FRAMEWORKS).document(framework_id).delete()


# ============================================================
# フレームワーク管理UI（render_consulting_ai_tab から呼ぶ）
# ============================================================
def _tab_framework_management(db, tenant_id: str) -> None:
    st.subheader("🗂️ フレームワーク管理")
    st.caption("分析に使うフレームワークの追加・有効化・削除ができます。")

    fw_list = _load_all_frameworks(db, tenant_id)

    # ── 一覧表示 & トグル
    st.markdown("#### 登録済みフレームワーク")
    if fw_list:
        import pandas as _pd
        rows = [
            {
                "framework_id": f.get("framework_id", ""),
                "名前":          f.get("name", ""),
                "説明":          f.get("description", ""),
                "有効":          "✅" if f.get("active", True) else "❌",
            }
            for f in fw_list
        ]
        st.dataframe(_pd.DataFrame(rows).drop(columns=["framework_id"]),
                     use_container_width=True, hide_index=True)

        # 有効/無効トグル
        st.markdown("**有効/無効 切り替え**")
        col_t1, col_t2, col_t3 = st.columns(3)
        fw_opts = {f.get("name", ""): f.get("framework_id", "") for f in fw_list}
        with col_t1:
            toggle_name = st.selectbox("フレームワーク", list(fw_opts.keys()),
                                        key="fw_toggle_sel")
        with col_t2:
            toggle_active = st.radio("状態", ["有効", "無効"], horizontal=True,
                                      key="fw_toggle_state")
        with col_t3:
            st.write("")
            st.write("")
            if st.button("🔄 切り替え", key="fw_toggle_btn", use_container_width=True):
                fid = fw_opts.get(toggle_name)
                if fid:
                    _toggle_framework_active(db, fid, toggle_active == "有効")
                    st.success(f"✅ '{toggle_name}' を {toggle_active} にしました")
                    st.rerun()

        # 削除
        st.markdown("**削除**")
        col_d1, col_d2 = st.columns([3, 1])
        with col_d1:
            del_name = st.selectbox("削除するフレームワーク", list(fw_opts.keys()),
                                     key="fw_del_sel")
        with col_d2:
            st.write("")
            st.write("")
            if st.button("🗑️ 削除", key="fw_del_btn",
                         use_container_width=True,
                         type="secondary"):
                fid = fw_opts.get(del_name)
                if fid:
                    _delete_consulting_framework(db, fid)
                    st.success(f"✅ '{del_name}' を削除しました")
                    st.rerun()
    else:
        st.info("フレームワークがまだ登録されていません。")

    st.markdown("---")

    # ── 新規登録
    st.markdown("#### ➕ 新規フレームワーク登録")
    new_name = st.text_input("名前 *", key="fw_new_name",
                              placeholder="例: ビジネスモデルキャンバス")
    new_desc = st.text_area("説明", key="fw_new_desc", height=80,
                             placeholder="このフレームワークの用途・概要")
    new_active = st.checkbox("登録後すぐ有効にする", value=True, key="fw_new_active")

    if st.button("💾 登録", key="fw_new_btn", type="primary",
                 use_container_width=True,
                 disabled=not bool((new_name or "").strip())):
        fid = _save_consulting_framework(
            db, tenant_id, new_name, new_desc, new_active
        )
        st.success(f"✅ '{new_name}' を登録しました (ID: `{fid}`)")
        st.rerun()


def _default_frameworks() -> list:
    return [
        {"framework_id": "purpose_means",    "name": "目的と手段",   "active": True},
        {"framework_id": "value_hierarchy",  "name": "価値の階層",   "active": True},
        {"framework_id": "customer_flow",    "name": "顧客導線",     "active": True},
        {"framework_id": "risk_return",      "name": "リスク・リターン", "active": True},
        {"framework_id": "edu_maturity",     "name": "教育成熟度",   "active": True},
        {"framework_id": "reduction_struct", "name": "還元構造",     "active": True},
    ]


# ============================================================
# RAG連携: 分析専用取得モード
# ============================================================
COL_KNOWLEDGE = "source_chunks"   # 本チャットRAGと同じグローバルコレクション

def _fetch_rag_context(db, tenant_id: str, query_text: str,
                       mode: str = "analysis",
                       limit: int = 5) -> dict:
    """
    分析専用RAG取得。
    mode: "analysis"  → consulting_analyses から過去分析を取得
          "knowledge" → source_chunks(investment_signal) から投資シグナルを取得
          "all"       → 両方取得してマージ
    戻り値: {"past_analyses": [...], "knowledge_chunks": [...]}
    """
    result: dict = {"past_analyses": [], "knowledge_chunks": []}

    # ── 過去分析取得
    if mode in ("analysis", "all"):
        try:
            snap = (
                db.collection(COL_ANALYSES)
                .where("tenant_id", "==", tenant_id)
                .limit(limit * 5)
                .stream()
            )
            docs = [s.to_dict() for s in snap]
            docs.sort(key=lambda d: str(d.get("created_at", "")), reverse=True)
            docs = docs[:limit]
            # 簡易キーワードフィルタ（query_text の先頭20字で絞り込み）
            kw = query_text[:20].lower()
            matched = [
                d for d in docs
                if kw in str(d.get("input_text", "")).lower()
            ]
            result["past_analyses"] = matched if matched else docs[:3]
        except Exception:
            pass

    # ── 投資シグナル取得（source_chunks から investment_signal を直接読む）
    # push_signals_to_rag が upsert_doc_chunks 経由で書き込む先がここ
    if mode in ("knowledge", "all"):
        try:
            snap = (
                db.collection("source_chunks")
                .where("source_type", "==", "investment_signal")
                .limit(limit * 10)
                .stream()
            )
            chunks_all = [s.to_dict() for s in snap]
            chunks_all.sort(key=lambda d: str(d.get("updated_at", "")), reverse=True)
            chunks_all = chunks_all[:limit * 5]

            # テナントの許可ソースに絞る（tenant_source_links を参照）
            try:
                links_snap = (
                    db.collection("tenant_source_links")
                    .where("tenant_id", "==", tenant_id)
                    .where("source_type", "==", "investment_signal")
                    .stream()
                )
                allowed_source_ids = {
                    l.to_dict().get("source_id")
                    for l in links_snap
                    if l.to_dict().get("enabled", True)
                }
                if allowed_source_ids:
                    chunks_all = [
                        c for c in chunks_all
                        if c.get("source_id") in allowed_source_ids
                    ]
            except Exception:
                pass  # フィルタ失敗時は全件使う（保険）

            # クエリキーワードで簡易フィルタ（銘柄名・コード・signal_type 優先）
            kw = query_text.lower()
            scored = []
            for c in chunks_all:
                txt = (str(c.get("text", "")) + " " + str(c.get("title", ""))).lower()
                hit = sum(1 for w in kw.split() if len(w) >= 2 and w in txt)
                scored.append((hit, c))
            scored.sort(key=lambda x: x[0], reverse=True)
            result["knowledge_chunks"] = [c for _, c in scored[:limit * 3]]
        except Exception:
            pass

    return result


def _rag_context_to_str(rag: dict) -> str:
    """RAG取得結果をプロンプト埋め込み用テキストに変換する。"""
    lines = []
    past = rag.get("past_analyses", [])
    if past:
        lines.append("【関連する過去分析】")
        for p in past[:3]:
            lines.append(
                f"- [{p.get('analysis_type','')}] "
                f"{str(p.get('input_text',''))[:60]}…"
                f"  → 要点: {str(p.get('result_json',{}).get('issue_summary',''))[:80]}"
            )
    chunks = rag.get("knowledge_chunks", [])
    if chunks:
        lines.append("【関連投資シグナル（直近データ）】")
        for c in chunks[:8]:
            # source_chunks のフィールド名に合わせて取得
            _text = str(c.get("text", ""))
            # text フィールドに全情報が入っているのでそのまま使う
            if _text:
                lines.append(_text[:200] + ("…" if len(_text) > 200 else ""))
            else:
                # fallback: タイトルのみ
                lines.append(f"- {str(c.get('title', c.get('source_id', '')))[:100]}")
    return "\n".join(lines) if lines else "（参照ソースなし）"


# ============================================================
# LLMプロンプト生成 & 呼び出し
# ============================================================
_COMMON_INSTRUCTION = """
【共通ルール】
- 感想ではなく分析を返すこと
- 抽象語だけで逃げないこと
- 不明点は missing_information に格納すること
- 推測は推測として扱うこと
- JSON以外の余計な文を絶対に返さないこと
- 目的と手段を混同しないこと
- 出力は必ず有効なJSONオブジェクトのみとすること（説明文・Markdownコードブロック不要）
"""

def _prompt_structure_diagnosis(input_text: str, supplement: str = "",
                                 doc_summary: str = "", memo: str = "",
                                 frameworks: list = None) -> str:
    fw_names = "、".join(f["name"] for f in (frameworks or []))
    return f"""
あなたは戦略コンサルタントです。以下の相談内容を構造診断してください。
適用フレームワーク: {fw_names or "目的と手段、価値の階層"}

【相談内容】
{input_text}

【補足情報】
{supplement or "（なし）"}

【添付資料要約】
{doc_summary or "（なし）"}

【任意メモ】
{memo or "（なし）"}

{_COMMON_INSTRUCTION}

【追加ルール（構造診断）】
- 現象・表層原因・根因を必ず分離すること
- 制約条件を必ず抽出すること
- 打ち手は優先順位順に返すこと

以下のJSONスキーマで返してください:
{{
  "issue_summary": "問題の要約（1〜2文）",
  "observations": ["観測事実1", "観測事実2"],
  "surface_causes": ["表層原因1", "表層原因2"],
  "root_causes": ["根因1", "根因2"],
  "constraints": ["制約1", "制約2"],
  "priority_points": ["優先論点1", "優先論点2"],
  "recommended_actions": ["打ち手1（優先度高）", "打ち手2", "打ち手3"],
  "risks": ["リスク1", "リスク2"],
  "missing_information": ["不足情報1", "不足情報2"]
}}
"""


def _prompt_issue_hypothesis(input_text: str, frameworks: list = None) -> str:
    fw_names = "、".join(f["name"] for f in (frameworks or []))
    return f"""
あなたは戦略コンサルタントです。以下の内容から論点・仮説を設計してください。
適用フレームワーク: {fw_names or "目的と手段、価値の階層"}

【入力内容】
{input_text}

{_COMMON_INSTRUCTION}

以下のJSONスキーマで返してください:
{{
  "main_issues": ["主要論点1", "主要論点2"],
  "hypotheses": ["仮説1", "仮説2"],
  "questions_to_verify": ["次に確認すべき質問1", "質問2"],
  "required_data": ["必要なデータ1", "データ2"],
  "decision_points": ["意思決定ポイント1", "ポイント2"]
}}
"""


def _prompt_comparison(options_raw: str, axes: list,
                        context: str = "") -> str:
    axes_str = "、".join(axes)
    return f"""
あなたは戦略コンサルタントです。以下の複数案を比較分析してください。

【比較対象案】
{options_raw}

【比較軸】
{axes_str}

【追加コンテキスト】
{context or "（なし）"}

{_COMMON_INSTRUCTION}

【追加ルール（比較表）】
- すべて同じ比較軸で比較すること
- 感覚論ではなく軸差で比較すること
- スコアは1〜5の整数で評価すること（5が最良）
- 最終推奨案を1つ返すこと

以下のJSONスキーマで返してください:
{{
  "comparison_axes": {json.dumps(axes, ensure_ascii=False)},
  "options": [
    {{
      "name": "案の名前",
      "scores": {{{", ".join(f'"{a}": 0' for a in axes)}}},
      "pros": ["長所1", "長所2"],
      "cons": ["短所1", "短所2"],
      "recommended_for": ["この案が向いているケース"]
    }}
  ],
  "final_recommendation": "最終推奨案と理由"
}}
"""


def _prompt_contradiction(strategy: str, policy: str = "",
                           chat_summary: str = "",
                           extra: str = "") -> str:
    return f"""
あなたは戦略コンサルタントです。以下の内容から矛盾・齟齬を検出してください。

【戦略文】
{strategy}

【方針文】
{policy or "（なし）"}

【会話要約】
{chat_summary or "（なし）"}

【任意資料】
{extra or "（なし）"}

{_COMMON_INSTRUCTION}

【追加ルール（矛盾検出）】
- 目的と手段の衝突を優先検出すること
- KPIと戦略のズレも検出対象とすること
- 矛盾がなければ contradictions を空配列にすること

以下のJSONスキーマで返してください:
{{
  "contradictions": [
    {{
      "type": "矛盾の種類（例: 目的手段衝突、KPIズレ、前提矛盾）",
      "description": "矛盾の具体的な説明",
      "why_problematic": "なぜ問題か",
      "fix_direction": "修正方向"
    }}
  ]
}}
"""


def _prompt_execution_plan(context: str, frameworks: list = None) -> str:
    fw_names = "、".join(f["name"] for f in (frameworks or []))
    return f"""
あなたは戦略コンサルタントです。以下の内容から実行プランを作成してください。
適用フレームワーク: {fw_names or "目的と手段"}

【内容】
{context}

{_COMMON_INSTRUCTION}

【追加ルール（実行プラン）】
- タスクは実行可能な粒度で分割すること
- 優先度は high / medium / low で分類すること
- KPIは可能な限り数値目標を含めること
- deadlineは相対的な目安で構わない（例: 2週間以内）

以下のJSONスキーマで返してください:
{{
  "action_plan": [
    {{
      "task": "タスク名",
      "owner": "担当者・部門",
      "deadline": "期限の目安",
      "kpi": "成功指標",
      "priority": "high"
    }}
  ]
}}
"""


def _call_consulting_llm(prompt: str, call_llm_fn: Callable,
                          tenant_id: str,
                          db=None,
                          rag_mode: str = "all",
                          rag_query: str = "") -> tuple[Any, str]:
    """
    LLMを呼び出してJSONパースまで行う。(parsed_dict, raw_text) を返す。
    db が渡された場合は RAG 取得を行い、プロンプト末尾に参照ソースを付加する。
    """
    sys_prompt = (
        "あなたは戦略コンサルタントです。"
        "出力は必ず有効なJSONオブジェクトのみ返してください。"
        "説明文・前置き・Markdownコードブロックは一切不要です。"
        "JSONだけを返してください。"
    )

    # ── RAG参照ソース付加
    if db is not None and rag_query:
        rag = _fetch_rag_context(db, tenant_id, rag_query, mode=rag_mode)
        rag_str = _rag_context_to_str(rag)
        prompt = prompt + f"\n\n【参照ソース（RAG取得）】\n{rag_str}"

    try:
        raw = call_llm_fn(prompt, history=[], sys=sys_prompt, tenant_id=tenant_id)
    except Exception as e:
        return None, f"LLM呼び出しエラー: {e}"
    parsed = _safe_parse_json(raw)
    return parsed, raw


# ============================================================
# 比較表ユーティリティ
# ============================================================
def _comparison_to_markdown(result: dict) -> str:
    options = result.get("options", [])
    axes = result.get("comparison_axes", DEFAULT_COMPARISON_AXES)
    if not options:
        return "（データなし）"
    header = "| 案 | " + " | ".join(axes) + " | 長所 | 短所 | 推奨ケース |"
    sep    = "|---|" + "---|" * len(axes) + "---|---|---|"
    rows   = []
    for opt in options:
        scores = opt.get("scores", {})
        score_cols = " | ".join(str(scores.get(a, "-")) for a in axes)
        pros = "、".join(opt.get("pros", []))
        cons = "、".join(opt.get("cons", []))
        rec  = "、".join(opt.get("recommended_for", []))
        rows.append(f"| {opt.get('name','')} | {score_cols} | {pros} | {cons} | {rec} |")
    return "\n".join([header, sep] + rows)


def _comparison_to_csv(result: dict) -> str:
    options = result.get("options", [])
    axes = result.get("comparison_axes", DEFAULT_COMPARISON_AXES)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["案"] + axes + ["長所", "短所", "推奨ケース"])
    for opt in options:
        scores = opt.get("scores", {})
        row = [opt.get("name", "")]
        row += [scores.get(a, "") for a in axes]
        row += [
            "・".join(opt.get("pros", [])),
            "・".join(opt.get("cons", [])),
            "・".join(opt.get("recommended_for", [])),
        ]
        writer.writerow(row)
    return buf.getvalue()


def _comparison_to_tsv(result: dict) -> str:
    csv_text = _comparison_to_csv(result)
    return csv_text.replace(",", "\t")


# ============================================================
# UI 部品
# ============================================================
def _render_history_sidebar(db, tenant_id: str, analysis_type: str) -> Optional[dict]:
    """過去の分析一覧をサイドエリアに表示し、選択した分析dictを返す。"""
    history = _load_analyses(db, tenant_id, analysis_type, limit=20)

    # ── 履歴テーブルを expander で常時表示
    type_label = ANALYSIS_TYPES.get(analysis_type, analysis_type)
    with st.expander(f"📂 {type_label}の保存履歴（{len(history)}件）", expanded=False):
        if not history:
            st.info("保存済みの分析はまだありません。")
        else:
            import pandas as _pd
            rows = [
                {
                    "analysis_id": h.get("analysis_id", ""),
                    "保存日時": str(h.get("created_at", ""))[:19],
                    "相談内容（抜粋）": str(h.get("input_text", ""))[:60],
                }
                for h in history
            ]
            st.dataframe(
                _pd.DataFrame(rows).drop(columns=["analysis_id"]),
                use_container_width=True,
                hide_index=True,
            )

    if not history:
        return None

    options = {
        f"{str(h.get('created_at',''))[:19]} | {str(h.get('input_text',''))[:40]}": h
        for h in history
    }
    selected_key = st.selectbox(
        "🔁 過去の結果を再表示する",
        ["（新規入力）"] + list(options.keys()),
        key=f"hist_sel_{analysis_type}",
    )
    if selected_key and selected_key != "（新規入力）":
        return options[selected_key]
    return None


def _render_json_download(result: dict, filename: str = "result.json") -> None:
    st.download_button(
        "⬇️ JSON保存",
        data=json.dumps(result, ensure_ascii=False, indent=2),
        file_name=filename,
        mime="application/json",
        key=f"dl_json_{filename}_{id(result)}",
    )


# ============================================================
# タブ1: 構造診断
# ============================================================
def _tab_structure_diagnosis(db, call_llm_fn: Callable,
                              tenant_id: str, user_id: str) -> None:
    st.subheader("🔍 構造診断")
    st.caption("相談内容から問題構造・根因・打ち手を抽出します。")

    # 過去履歴
    past = _render_history_sidebar(db, tenant_id, "structure_diagnosis")

    if past:
        st.markdown("---")
        st.markdown("**📄 再表示：過去の構造診断**")
        _render_structure_result(past.get("result_json", {}))
        _render_json_download(past.get("result_json", {}), "structure_diagnosis.json")
        return

    # フレームワーク選択
    fw_all = _load_frameworks(db, tenant_id)
    fw_names = [f["name"] for f in fw_all]
    selected_fw_names = st.multiselect("適用フレームワーク", fw_names,
                                        default=fw_names[:2] if len(fw_names) >= 2 else fw_names,
                                        key="sd_fw")
    selected_fw = [f for f in fw_all if f["name"] in selected_fw_names]

    # 入力フォーム
    input_text = st.text_area("📝 相談内容 *", height=120,
                               placeholder="例: 売上が3ヶ月連続で前年比-15%。広告費を増やしたが改善なし。",
                               key="sd_input")
    supplement = st.text_area("補足情報", height=80,
                               placeholder="例: 競合は値引き攻勢中。自社は品質路線。",
                               key="sd_supplement")
    col_a, col_b = st.columns(2)
    with col_a:
        doc_summary = st.text_area("添付資料要約", height=80,
                                    placeholder="資料の要点を記載",
                                    key="sd_doc")
    with col_b:
        memo = st.text_area("任意メモ", height=80,
                             placeholder="気になる点など",
                             key="sd_memo")

    if st.button("🧠 構造診断を実行", type="primary",
                 use_container_width=True, key="sd_run",
                 disabled=not bool((input_text or "").strip())):
        with st.spinner("分析中…"):
            prompt = _prompt_structure_diagnosis(
                input_text, supplement, doc_summary, memo, selected_fw
            )
            parsed, raw = _call_consulting_llm(
                prompt, call_llm_fn, tenant_id,
                db=db, rag_mode="all", rag_query=input_text
            )

        if parsed is None:
            st.error("JSON解析に失敗しました。LLM出力:")
            st.code(raw[:2000])
            return

        analysis_id = _save_analysis(
            db, tenant_id, user_id,
            "structure_diagnosis", input_text, parsed
        )
        st.success(f"✅ 保存完了 (analysis_id: `{analysis_id}`)")
        _render_structure_result(parsed)
        _render_json_download(parsed, "structure_diagnosis.json")


def _render_structure_result(r: dict) -> None:
    if not r:
        st.warning("結果データがありません。")
        return
    st.markdown(f"### 📌 問題要約\n{r.get('issue_summary','')}")
    _render_list_section("👁️ 観測事実",       r.get("observations", []))
    _render_list_section("🌊 表層原因",        r.get("surface_causes", []))
    _render_list_section("🔑 根因",            r.get("root_causes", []))
    _render_list_section("⛓️ 制約条件",        r.get("constraints", []))
    _render_list_section("🎯 優先論点",        r.get("priority_points", []))
    _render_list_section("🚀 推奨打ち手",      r.get("recommended_actions", []))
    _render_list_section("⚠️ リスク",          r.get("risks", []))
    _render_list_section("❓ 不足情報",        r.get("missing_information", []))


# ============================================================
# タブ2: 論点・仮説設計
# ============================================================
def _tab_issue_hypothesis(db, call_llm_fn: Callable,
                           tenant_id: str, user_id: str) -> None:
    st.subheader("💡 論点・仮説設計")
    st.caption("問いを構造化し、次のアクションを明確にします。")

    past = _render_history_sidebar(db, tenant_id, "issue_hypothesis")
    if past:
        st.markdown("---")
        st.markdown("**📄 再表示：過去の論点設計**")
        _render_issue_result(past.get("result_json", {}))
        _render_json_download(past.get("result_json", {}), "issue_hypothesis.json")
        return

    fw_all = _load_frameworks(db, tenant_id)
    fw_names = [f["name"] for f in fw_all]
    selected_fw_names = st.multiselect("適用フレームワーク", fw_names,
                                        default=fw_names[:2] if len(fw_names) >= 2 else fw_names,
                                        key="ih_fw")
    selected_fw = [f for f in fw_all if f["name"] in selected_fw_names]

    input_text = st.text_area("📝 分析対象の内容 *", height=150,
                               placeholder="例: 新規事業の方向性を検討中。候補はA/B/Cの3案。",
                               key="ih_input")

    if st.button("🧠 論点・仮説を設計", type="primary",
                 use_container_width=True, key="ih_run",
                 disabled=not bool((input_text or "").strip())):
        with st.spinner("設計中…"):
            prompt = _prompt_issue_hypothesis(input_text, selected_fw)
            parsed, raw = _call_consulting_llm(
                prompt, call_llm_fn, tenant_id,
                db=db, rag_mode="all", rag_query=input_text
            )

        if parsed is None:
            st.error("JSON解析に失敗しました。LLM出力:")
            st.code(raw[:2000])
            return

        analysis_id = _save_analysis(
            db, tenant_id, user_id,
            "issue_hypothesis", input_text, parsed
        )
        st.success(f"✅ 保存完了 (analysis_id: `{analysis_id}`)")
        _render_issue_result(parsed)
        _render_json_download(parsed, "issue_hypothesis.json")


def _render_issue_result(r: dict) -> None:
    if not r:
        st.warning("結果データがありません。")
        return
    _render_list_section("🎯 主要論点",            r.get("main_issues", []))
    _render_list_section("💭 仮説一覧",             r.get("hypotheses", []))
    _render_list_section("🔍 次に確認すべき質問",  r.get("questions_to_verify", []))
    _render_list_section("📊 必要なデータ",         r.get("required_data", []))
    _render_list_section("🔀 意思決定ポイント",     r.get("decision_points", []))


# ============================================================
# タブ3: 比較表
# ============================================================
def _tab_comparison(db, call_llm_fn: Callable,
                    tenant_id: str, user_id: str) -> None:
    st.subheader("📊 比較表生成")
    st.caption("複数案を同一軸で比較し、最終推奨を導出します。")

    past = _render_history_sidebar(db, tenant_id, "comparison")
    if past:
        st.markdown("---")
        st.markdown("**📄 再表示：過去の比較表**")
        _render_comparison_result(past.get("result_json", {}))
        return

    # 比較軸設定
    axes_input = st.text_input(
        "比較軸（カンマ区切り）",
        value="、".join(DEFAULT_COMPARISON_AXES),
        key="cmp_axes"
    )
    axes = [a.strip() for a in re.split(r"[,、，]", axes_input) if a.strip()]
    if not axes:
        axes = DEFAULT_COMPARISON_AXES[:]

    options_raw = st.text_area(
        "📝 比較する案（1案1行で記述）*",
        height=120,
        placeholder="案A: コスト削減路線 — 人件費15%削減、外注化\n案B: 収益拡大路線 — 新商品投入、EC強化\n案C: 現状維持 — コスト・投資ともに据え置き",
        key="cmp_options"
    )
    context = st.text_area("追加コンテキスト", height=80,
                            placeholder="例: 現在のキャッシュフローは3ヶ月分。", key="cmp_ctx")

    if st.button("🧠 比較表を生成", type="primary",
                 use_container_width=True, key="cmp_run",
                 disabled=not bool((options_raw or "").strip())):
        with st.spinner("生成中…"):
            prompt = _prompt_comparison(options_raw, axes, context)
            parsed, raw = _call_consulting_llm(
                prompt, call_llm_fn, tenant_id,
                db=db, rag_mode="all", rag_query=options_raw
            )

        if parsed is None:
            st.error("JSON解析に失敗しました。LLM出力:")
            st.code(raw[:2000])
            return

        analysis_id = _save_analysis(
            db, tenant_id, user_id,
            "comparison", options_raw, parsed
        )
        st.success(f"✅ 保存完了 (analysis_id: `{analysis_id}`)")
        _render_comparison_result(parsed)


def _render_comparison_result(r: dict) -> None:
    if not r:
        st.warning("結果データがありません。")
        return

    options = r.get("options", [])
    axes = r.get("comparison_axes", DEFAULT_COMPARISON_AXES)

    # ── Markdown表表示
    md_table = _comparison_to_markdown(r)
    st.markdown("#### 📋 比較表")
    st.markdown(md_table)

    # ── 並び替え & フィルタ
    if options:
        import pandas as _pd
        rows = []
        for opt in options:
            row = {"案": opt.get("name", "")}
            for ax in axes:
                row[ax] = opt.get("scores", {}).get(ax, 0)
            row["長所"] = "・".join(opt.get("pros", []))
            row["短所"] = "・".join(opt.get("cons", []))
            rows.append(row)
        df = _pd.DataFrame(rows)

        # ── フィルタUI
        st.markdown("**🔎 フィルタ**")
        col_f1, col_f2, col_f3 = st.columns(3)

        # 案名フィルタ
        all_option_names = df["案"].tolist()
        with col_f1:
            selected_options = st.multiselect(
                "表示する案",
                options=all_option_names,
                default=all_option_names,
                key="cmp_filter_options",
            )

        # スコア下限フィルタ（並び替え軸のスコアが閾値以上の案のみ表示）
        with col_f2:
            filter_axis = st.selectbox("フィルタ対象軸", axes, key="cmp_filter_axis")
        with col_f3:
            score_min = st.slider(
                "最低スコア", min_value=1, max_value=5, value=1,
                key="cmp_filter_score_min"
            )

        # フィルタ適用
        df_filtered = df[
            df["案"].isin(selected_options) &
            (df[filter_axis] >= score_min)
        ] if selected_options else df

        # ── 並び替え
        st.markdown("**🔀 並び替え**")
        col_s1, col_s2 = st.columns(2)
        with col_s1:
            sort_axis = st.selectbox("並び替え軸", axes, key="cmp_sort")
        with col_s2:
            ascending = st.checkbox("昇順", value=False, key="cmp_asc")

        df_sorted = df_filtered.sort_values(sort_axis, ascending=ascending)
        st.caption(f"表示件数: {len(df_sorted)} / {len(df)} 案")
        st.dataframe(df_sorted, use_container_width=True, hide_index=True)

    # ── 最終推奨
    rec = r.get("final_recommendation", "")
    if rec:
        st.success(f"### 🏆 最終推奨\n{rec}")

    # ── ダウンロード
    col1, col2, col3 = st.columns(3)
    csv_data = _comparison_to_csv(r)
    tsv_data = _comparison_to_tsv(r)
    json_data = json.dumps(r, ensure_ascii=False, indent=2)
    with col1:
        st.download_button("⬇️ CSV", data=csv_data,
                           file_name="comparison.csv", mime="text/csv",
                           key=f"dl_csv_{id(r)}")
    with col2:
        st.download_button("⬇️ TSV", data=tsv_data,
                           file_name="comparison.tsv", mime="text/tab-separated-values",
                           key=f"dl_tsv_{id(r)}")
    with col3:
        st.download_button("⬇️ JSON", data=json_data,
                           file_name="comparison.json", mime="application/json",
                           key=f"dl_json2_{id(r)}")


# ============================================================
# タブ4: 矛盾検出
# ============================================================
def _tab_contradiction(db, call_llm_fn: Callable,
                        tenant_id: str, user_id: str) -> None:
    st.subheader("⚡ 矛盾検出")
    st.caption("戦略・方針・会話の中の矛盾・齟齬を検出します。")

    past = _render_history_sidebar(db, tenant_id, "contradiction_check")
    if past:
        st.markdown("---")
        st.markdown("**📄 再表示：過去の矛盾検出**")
        _render_contradiction_result(past.get("result_json", {}))
        _render_json_download(past.get("result_json", {}), "contradiction.json")
        return

    strategy = st.text_area("📝 戦略文 *", height=100,
                             placeholder="例: 今期は高付加価値路線で粗利率45%以上を目指す。",
                             key="ct_strategy")
    policy = st.text_area("方針文", height=80,
                           placeholder="例: コスト削減最優先。全部門10%カット。",
                           key="ct_policy")
    col_a, col_b = st.columns(2)
    with col_a:
        chat_summary = st.text_area("会話要約", height=80,
                                     placeholder="直近の会議・チャットの要点",
                                     key="ct_chat")
    with col_b:
        extra = st.text_area("任意資料", height=80,
                              placeholder="KPI設定・予算計画など",
                              key="ct_extra")

    if st.button("🧠 矛盾を検出", type="primary",
                 use_container_width=True, key="ct_run",
                 disabled=not bool((strategy or "").strip())):
        with st.spinner("検出中…"):
            prompt = _prompt_contradiction(strategy, policy, chat_summary, extra)
            parsed, raw = _call_consulting_llm(
                prompt, call_llm_fn, tenant_id,
                db=db, rag_mode="all", rag_query=strategy
            )

        if parsed is None:
            st.error("JSON解析に失敗しました。LLM出力:")
            st.code(raw[:2000])
            return

        analysis_id = _save_analysis(
            db, tenant_id, user_id,
            "contradiction_check", strategy, parsed
        )
        st.success(f"✅ 保存完了 (analysis_id: `{analysis_id}`)")
        _render_contradiction_result(parsed)
        _render_json_download(parsed, "contradiction.json")


def _render_contradiction_result(r: dict) -> None:
    if not r:
        st.warning("結果データがありません。")
        return
    contradictions = r.get("contradictions", [])
    if not contradictions:
        st.success("✅ 矛盾は検出されませんでした。")
        return
    st.warning(f"⚡ {len(contradictions)} 件の矛盾を検出しました")
    for i, c in enumerate(contradictions, 1):
        with st.expander(f"矛盾 {i}: {c.get('type','')}", expanded=True):
            st.markdown(f"**内容:** {c.get('description','')}")
            st.markdown(f"**なぜ問題か:** {c.get('why_problematic','')}")
            st.markdown(f"**修正方向:** {c.get('fix_direction','')}")


# ============================================================
# タブ5: 実行プラン
# ============================================================
def _tab_execution_plan(db, call_llm_fn: Callable,
                         tenant_id: str, user_id: str) -> None:
    st.subheader("📋 実行プラン化")
    st.caption("分析内容をタスク・KPI・期限付きの実行プランに変換します。")

    inner_tab_gen, inner_tab_manage = st.tabs(["🆕 プラン生成", "📌 実行管理"])

    with inner_tab_gen:
        past = _render_history_sidebar(db, tenant_id, "execution_plan")
        if past:
            st.markdown("---")
            st.markdown("**📄 再表示：過去の実行プラン**")
            _render_execution_result(past.get("result_json", {}))
            _render_json_download(past.get("result_json", {}), "execution_plan.json")
        else:
            fw_all = _load_frameworks(db, tenant_id)
            fw_names = [f["name"] for f in fw_all]
            selected_fw_names = st.multiselect("適用フレームワーク", fw_names,
                                                default=fw_names[:1] if fw_names else [],
                                                key="ep_fw")
            selected_fw = [f for f in fw_all if f["name"] in selected_fw_names]

            context = st.text_area(
                "📝 実行プランを作成したい内容 *", height=150,
                placeholder="例: 構造診断の結果 or 比較表の推奨案 or 方針をここに貼り付け",
                key="ep_context"
            )

            if st.button("🧠 実行プランを生成", type="primary",
                         use_container_width=True, key="ep_run",
                         disabled=not bool((context or "").strip())):
                with st.spinner("生成中…"):
                    prompt = _prompt_execution_plan(context, selected_fw)
                    parsed, raw = _call_consulting_llm(
                        prompt, call_llm_fn, tenant_id,
                        db=db, rag_mode="all", rag_query=context
                    )

                if parsed is None:
                    st.error("JSON解析に失敗しました。LLM出力:")
                    st.code(raw[:2000])
                    return

                analysis_id = _save_analysis(
                    db, tenant_id, user_id,
                    "execution_plan", context, parsed
                )
                # 実行計画はアクションプランとしても保存
                action_plan = parsed.get("action_plan", [])
                if action_plan:
                    _save_action_plan(db, tenant_id, analysis_id, action_plan)
                st.success(f"✅ 保存完了 (analysis_id: `{analysis_id}`, タスク数: {len(action_plan)})")
                _render_execution_result(parsed)
                _render_json_download(parsed, "execution_plan.json")

    with inner_tab_manage:
        st.markdown("#### 🗂️ 実行タスク管理")
        plans = _load_action_plans(db, tenant_id, limit=50)
        if not plans:
            st.info("実行プランがまだありません。「プラン生成」タブで作成してください。")
            return

        import pandas as _pd
        STATUS_OPTIONS = ["open", "in_progress", "done", "cancelled"]
        PRIORITY_COLOR = {"high": "🔴", "medium": "🟡", "low": "🟢"}

        rows = []
        for p in plans:
            rows.append({
                "plan_id":  p.get("plan_id", ""),
                "タスク":   p.get("task", ""),
                "担当":     p.get("owner", ""),
                "期限":     p.get("deadline", ""),
                "KPI":      p.get("kpi", ""),
                "優先度":   PRIORITY_COLOR.get(p.get("priority","medium"), "🟡") + " " + p.get("priority",""),
                "状態":     p.get("status", "open"),
                "作成日":   str(p.get("created_at",""))[:10],
            })
        df = _pd.DataFrame(rows)

        # フィルタ
        col_f1, col_f2 = st.columns(2)
        with col_f1:
            status_filter = st.multiselect("状態フィルタ", STATUS_OPTIONS,
                                            default=["open","in_progress"],
                                            key="ep_status_filter")
        with col_f2:
            priority_filter = st.multiselect("優先度フィルタ",
                                              ["high","medium","low"],
                                              default=["high","medium","low"],
                                              key="ep_priority_filter")

        df_filtered = df[
            df["状態"].isin(status_filter) &
            df["優先度"].str.contains("|".join(priority_filter), na=False)
        ] if status_filter and priority_filter else df

        st.dataframe(df_filtered.drop(columns=["plan_id"]),
                     use_container_width=True, hide_index=True)

        # ステータス更新
        st.markdown("---")
        st.markdown("**ステータス更新**")
        col_u1, col_u2, col_u3 = st.columns(3)
        with col_u1:
            update_plan_opts = {
                f"{r['タスク'][:30]}": r["plan_id"]
                for _, r in df_filtered.iterrows()
                if r.get("plan_id")
            }
            selected_task_label = st.selectbox("タスク選択", list(update_plan_opts.keys()),
                                                key="ep_update_task")
        with col_u2:
            new_status = st.selectbox("新しいステータス", STATUS_OPTIONS,
                                       key="ep_update_status")
        with col_u3:
            if st.button("🔄 更新", key="ep_update_btn",
                         use_container_width=True,
                         disabled=not bool(update_plan_opts)):
                pid = update_plan_opts.get(selected_task_label)
                if pid:
                    _update_plan_status(db, pid, new_status)
                    st.success(f"✅ ステータスを '{new_status}' に更新しました")
                    st.rerun()


def _render_execution_result(r: dict) -> None:
    if not r:
        st.warning("結果データがありません。")
        return
    tasks = r.get("action_plan", [])
    if not tasks:
        st.info("タスクが生成されませんでした。")
        return
    PRIORITY_COLOR = {"high": "🔴", "medium": "🟡", "low": "🟢"}
    for i, task in enumerate(tasks, 1):
        prio = task.get("priority", "medium")
        icon = PRIORITY_COLOR.get(prio, "🟡")
        with st.expander(f"{icon} {i}. {task.get('task','')}", expanded=(prio=="high")):
            col_a, col_b = st.columns(2)
            with col_a:
                st.markdown(f"**担当:** {task.get('owner','')}")
                st.markdown(f"**期限:** {task.get('deadline','')}")
            with col_b:
                st.markdown(f"**KPI:** {task.get('kpi','')}")
                st.markdown(f"**優先度:** {prio}")


# ============================================================
# 共通ユーティリティ
# ============================================================
def _render_list_section(title: str, items: list) -> None:
    if not items:
        return
    with st.expander(title, expanded=True):
        for item in items:
            st.markdown(f"- {item}")


# ============================================================
# メインエントリーポイント（app.py から呼ぶ）
# ============================================================
def render_consulting_ai_tab(db, call_llm_fn: Callable,
                              authed_uid_fn: Callable,
                              default_tenant: str) -> None:
    """
    app.py の admin_menu == "🔬 コンサルAI" ブロックから呼ぶ。

    Parameters
    ----------
    db              : firestore.Client  (app.py の db をそのまま渡す)
    call_llm_fn     : callable          (app.py の call_llm 関数)
    authed_uid_fn   : callable          (app.py の authed_uid 関数)
    default_tenant  : str               (app.py の DEFAULT_TENANT)
    """
    st.header("🔬 コンサルAI")
    st.caption("構造診断 / 論点設計 / 比較表 / 矛盾検出 / 実行プランを統合したコンサルティングAI機能")

    # テナント選択
    try:
        from app import list_tenants  # noqa: F401 – graceful fallback
        _tenants = [t["tenant_id"] for t in list_tenants()]
    except Exception:
        _tenants = [default_tenant]

    if not _tenants:
        _tenants = [default_tenant]

    tenant_id = st.selectbox("テナント", _tenants,
                              index=0, key="consulting_tenant_sel")
    user_id = authed_uid_fn() or "admin"

    st.divider()

    tabs = st.tabs([
        "🔍 構造診断",
        "💡 論点・仮説",
        "📊 比較表",
        "⚡ 矛盾検出",
        "📋 実行プラン",
        "🗂️ FW管理",          # ← 追加
    ])

    with tabs[0]:
        try:
            _tab_structure_diagnosis(db, call_llm_fn, tenant_id, user_id)
        except Exception as _e:
            st.error(f"構造診断エラー: {_e}")

    with tabs[1]:
        try:
            _tab_issue_hypothesis(db, call_llm_fn, tenant_id, user_id)
        except Exception as _e:
            st.error(f"論点設計エラー: {_e}")

    with tabs[2]:
        try:
            _tab_comparison(db, call_llm_fn, tenant_id, user_id)
        except Exception as _e:
            st.error(f"比較表エラー: {_e}")

    with tabs[3]:
        try:
            _tab_contradiction(db, call_llm_fn, tenant_id, user_id)
        except Exception as _e:
            st.error(f"矛盾検出エラー: {_e}")

    with tabs[4]:
        try:
            _tab_execution_plan(db, call_llm_fn, tenant_id, user_id)
        except Exception as _e:
            st.error(f"実行プランエラー: {_e}")

    with tabs[5]:                                          # ← 追加
        try:
            _tab_framework_management(db, tenant_id)
        except Exception as _e:
            st.error(f"FW管理エラー: {_e}")
