# ascend_consulting_inquiry.py
# ============================================================
# ASCEND 個人相談モジュール
# app.py と同階層に配置し、app.py から import して使う
#
# 【絶対ルール】
# - chat_sessions / messages と混ぜない
# - chat_summary と混ぜない
# - teacher generation の対象にしない
# - lgbm_training_logs と混ぜない
# - 通常チャットUIに埋め込まず、独立導線で使う
# ============================================================

from __future__ import annotations

import datetime
import uuid
from typing import Optional

import streamlit as st

# ============================================================
# コレクション名定数（既存コレクションとは完全に独立）
# ============================================================
COL_INQUIRIES = "consulting_inquiries"
COL_INQ_MESSAGES = "consulting_inquiry_messages"

# ============================================================
# ステータス定義
# ============================================================
INQUIRY_STATUSES = {
    "new":          "未対応",
    "in_progress":  "対応中",
    "replied":      "返信済み",
    "waiting_user": "返信待ち",
    "closed":       "完了",
}

STATUS_BADGE_COLOR = {
    "new":          "#ef4444",
    "in_progress":  "#f59e0b",
    "replied":      "#3b82f6",
    "waiting_user": "#8b5cf6",
    "closed":       "#6b7280",
}

CATEGORY_OPTIONS = [
    "戦略・方針相談",
    "売上・マーケティング",
    "組織・人材",
    "財務・資金調達",
    "オペレーション改善",
    "その他",
]


# ============================================================
# コレクション参照ヘルパー
# ============================================================
def consulting_inquiries_col(db):
    """consulting_inquiries コレクションへの参照を返す。"""
    return db.collection(COL_INQUIRIES)


def consulting_inquiry_messages_col(db):
    """consulting_inquiry_messages コレクションへの参照を返す。"""
    return db.collection(COL_INQ_MESSAGES)


# ============================================================
# CRUD: 相談本体
# ============================================================
def create_consulting_inquiry(
    db,
    tenant_id: str,
    user_id: str,
    user_display_name: str,
    title: str,
    body: str,
    category: str = "",
    supplement: str = "",
) -> str:
    """
    新規相談スレッドと初回メッセージを同時に作成する。
    戻り値: inquiry_id
    """
    inquiry_id = str(uuid.uuid4())
    now = datetime.datetime.utcnow()

    inquiry_doc = {
        "inquiry_id":        inquiry_id,
        "tenant_id":         tenant_id,
        "user_id":           user_id,
        "user_display_name": user_display_name or user_id,
        "title":             title.strip(),
        "category":          category,
        "supplement":        supplement.strip(),
        "status":            "new",
        "created_at":        now,
        "updated_at":        now,
        "last_message_at":   now,
        "last_sender_type":  "user",
        "unread_for_admin":  True,
        "unread_for_user":   False,
        "admin_memo":        "",
        "assigned_admin_id": "",
        "is_deleted":        False,
    }
    consulting_inquiries_col(db).document(inquiry_id).set(inquiry_doc)

    # 初回メッセージを作成
    add_consulting_inquiry_message(
        db=db,
        inquiry_id=inquiry_id,
        tenant_id=tenant_id,
        sender_type="user",
        sender_id=user_id,
        body=body,
    )
    return inquiry_id


def add_consulting_inquiry_message(
    db,
    inquiry_id: str,
    tenant_id: str,
    sender_type: str,  # "user" or "admin"
    sender_id: str,
    body: str,
    visible_to_user: bool = True,
    visible_to_admin: bool = True,
) -> str:
    """
    相談にメッセージを追加し、本体の last_message_at / unread / status を更新する。
    戻り値: message_id
    """
    message_id = str(uuid.uuid4())
    now = datetime.datetime.utcnow()

    msg_doc = {
        "message_id":       message_id,
        "inquiry_id":       inquiry_id,
        "tenant_id":        tenant_id,
        "sender_type":      sender_type,
        "sender_id":        sender_id,
        "body":             body.strip(),
        "created_at":       now,
        "visible_to_user":  visible_to_user,
        "visible_to_admin": visible_to_admin,
        "attachment_meta":  {},
        "is_deleted":       False,
    }
    consulting_inquiry_messages_col(db).document(message_id).set(msg_doc)

    # 本体を更新
    update_fields: dict = {
        "last_message_at":  now,
        "updated_at":       now,
        "last_sender_type": sender_type,
    }

    if sender_type == "admin":
        update_fields["unread_for_user"]  = True
        update_fields["unread_for_admin"] = False
        update_fields["status"]           = "replied"
    else:
        update_fields["unread_for_admin"] = True
        update_fields["unread_for_user"]  = False
        # ユーザーが返信 → 管理側対応待ち
        update_fields["status"] = "in_progress"

    consulting_inquiries_col(db).document(inquiry_id).update(update_fields)
    return message_id


def get_consulting_inquiry(db, inquiry_id: str) -> Optional[dict]:
    """1件の相談本体を取得する。存在しなければ None を返す。"""
    snap = consulting_inquiries_col(db).document(inquiry_id).get()
    if snap.exists:
        return snap.to_dict()
    return None


def list_user_consulting_inquiries(
    db,
    tenant_id: str,
    user_id: str,
    limit: int = 50,
) -> list:
    """
    指定ユーザーの相談一覧を取得する（is_deleted=False のみ）。
    ユーザーは自分の相談だけ見える。
    複合インデックス不要：user_id の単一フィールドのみで Firestore クエリし、
    残りは Python 側でフィルタ。
    """
    try:
        snap = (
            consulting_inquiries_col(db)
            .where("user_id", "==", user_id)
            .stream()
        )
        results = [s.to_dict() for s in snap]
    except Exception as e:
        st.warning(f"[個人相談] ユーザー相談取得エラー: {e}")
        return []

    # Python側でフィルタ（複合インデックス不要）
    results = [
        r for r in results
        if not r.get("is_deleted", False)
        and r.get("tenant_id") == tenant_id
    ]
    results.sort(
        key=lambda x: (x.get("last_message_at") or datetime.datetime.min).replace(tzinfo=None) if hasattr((x.get("last_message_at") or datetime.datetime.min), "replace") else datetime.datetime.min,
        reverse=True,
    )
    return results[:limit]


def list_admin_consulting_inquiries(
    db,
    tenant_id: str,
    status_filter: Optional[str] = None,
    unread_only: bool = False,
    search_keyword: str = "",
    limit: int = 100,
) -> list:
    """
    管理側: 全相談一覧を取得する。
    複合インデックス不要：is_deleted / status などは Python 側でフィルタ。
    tenant_id は DEFAULT_TENANT のほか、全テナントも対象にするため
    Firestore クエリではフィルタせず Python 側で絞る（admin は全テナント閲覧可）。
    """
    try:
        snap = consulting_inquiries_col(db).stream()
        results = [s.to_dict() for s in snap]
    except Exception as e:
        st.warning(f"[個人相談] 管理一覧取得エラー: {e}")
        return []

    # Python側フィルタ（複合インデックス不要・テナント不一致も拾う）
    results = [r for r in results if not r.get("is_deleted", False)]

    if status_filter:
        results = [r for r in results if r.get("status") == status_filter]
    if unread_only:
        results = [r for r in results if r.get("unread_for_admin", False)]
    if search_keyword:
        kw = search_keyword.lower()
        results = [
            r for r in results
            if kw in (r.get("title") or "").lower()
            or kw in (r.get("user_id") or "").lower()
            or kw in (r.get("user_display_name") or "").lower()
        ]

    results.sort(
        key=lambda x: (x.get("last_message_at") or datetime.datetime.min).replace(tzinfo=None) if hasattr((x.get("last_message_at") or datetime.datetime.min), "replace") else datetime.datetime.min,
        reverse=True,
    )
    return results[:limit]


def list_consulting_inquiry_messages(
    db,
    inquiry_id: str,
    visible_to: str = "both",  # "user" / "admin" / "both"
    limit: int = 200,
) -> list:
    """指定相談のメッセージ一覧を取得する（昇順）。
    複合インデックス不要：inquiry_id のみ Firestore フィルタ、残りは Python 側で処理。
    """
    try:
        snap = (
            consulting_inquiry_messages_col(db)
            .where("inquiry_id", "==", inquiry_id)
            .stream()
        )
        msgs = [s.to_dict() for s in snap]
    except Exception as e:
        st.warning(f"[個人相談] メッセージ取得エラー: {e}")
        return []

    # Python側フィルタ（複合インデックス不要）
    msgs = [m for m in msgs if not m.get("is_deleted", False)]
    msgs.sort(key=lambda x: (x.get("created_at") or datetime.datetime.min).replace(tzinfo=None) if hasattr((x.get("created_at") or datetime.datetime.min), "replace") else datetime.datetime.min)
    msgs = msgs[:limit]

    if visible_to == "user":
        msgs = [m for m in msgs if m.get("visible_to_user", True)]
    elif visible_to == "admin":
        msgs = [m for m in msgs if m.get("visible_to_admin", True)]

    return msgs


def update_consulting_inquiry_status(db, inquiry_id: str, new_status: str) -> None:
    """ステータスを更新する。"""
    consulting_inquiries_col(db).document(inquiry_id).update({
        "status":     new_status,
        "updated_at": datetime.datetime.utcnow(),
    })


def update_consulting_admin_memo(db, inquiry_id: str, memo: str) -> None:
    """管理メモを保存する。"""
    consulting_inquiries_col(db).document(inquiry_id).update({
        "admin_memo": memo,
        "updated_at": datetime.datetime.utcnow(),
    })


def mark_inquiry_read_for_admin(db, inquiry_id: str) -> None:
    """管理側が詳細を開いた時に既読にする。"""
    consulting_inquiries_col(db).document(inquiry_id).update({
        "unread_for_admin": False,
        "updated_at":       datetime.datetime.utcnow(),
    })


def mark_inquiry_read_for_user(db, inquiry_id: str) -> None:
    """ユーザーが詳細を開いた時に既読にする。"""
    consulting_inquiries_col(db).document(inquiry_id).update({
        "unread_for_user": False,
        "updated_at":      datetime.datetime.utcnow(),
    })


def update_consulting_assigned_admin(db, inquiry_id: str, admin_id: str) -> None:
    """担当者を設定する。"""
    consulting_inquiries_col(db).document(inquiry_id).update({
        "assigned_admin_id": admin_id,
        "updated_at":        datetime.datetime.utcnow(),
    })


# ============================================================
# 補助関数
# ============================================================
def normalize_inquiry_status(status: str) -> str:
    """内部ステータス値を表示用ラベルに変換する。"""
    return INQUIRY_STATUSES.get(status, status)


def can_view_inquiry(inquiry: dict, user_id: str, is_admin: bool) -> bool:
    """ユーザーがこの相談を閲覧できるか判定する。"""
    if is_admin:
        return True
    return inquiry.get("user_id") == user_id


def _fmt_dt(dt) -> str:
    """datetime を JST 表示文字列に変換する。"""
    if dt is None:
        return "—"
    if not isinstance(dt, datetime.datetime):
        try:
            dt = datetime.datetime.fromisoformat(str(dt))
        except Exception:
            return str(dt)[:16]
    try:
        jst = datetime.timezone(datetime.timedelta(hours=9))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone(jst).strftime("%Y/%m/%d %H:%M")
    except Exception:
        return str(dt)[:16]


def build_inquiry_summary_row(inq: dict) -> dict:
    """一覧表示用の行データを組み立てる。"""
    unread_admin = inq.get("unread_for_admin", False)
    unread_user  = inq.get("unread_for_user", False)
    status_key   = inq.get("status", "new")
    last_sender  = inq.get("last_sender_type", "user")
    return {
        "inquiry_id":       inq.get("inquiry_id", ""),
        "タイトル":          inq.get("title", "（無題）"),
        "カテゴリ":          inq.get("category", ""),
        "ステータス":        normalize_inquiry_status(status_key),
        "_status_key":      status_key,
        "最終更新":          _fmt_dt(inq.get("last_message_at")),
        "最終送信者":        "管理側" if last_sender == "admin" else "あなた",
        "未読(管理)":        "🔴 未読" if unread_admin else "",
        "未読(ユーザー)":    "🔵 未読" if unread_user else "",
        "ユーザーID":        inq.get("user_id", ""),
        "表示名":            inq.get("user_display_name", ""),
    }


def _status_badge(status_key: str) -> str:
    color = STATUS_BADGE_COLOR.get(status_key, "#6b7280")
    label = normalize_inquiry_status(status_key)
    return (
        f'<span style="background:{color};color:#fff;'
        f'font-size:0.72rem;padding:2px 8px;border-radius:12px;'
        f'font-weight:600;">{label}</span>'
    )


# ============================================================
# UI: ユーザー側
# ============================================================
def render_user_consulting_inquiry_page(
    db,
    uid: str,
    tenant_id: str,
    display_name: str = "",
) -> None:
    """
    ユーザー向け個人相談ページ全体を描画する。
    app.py のユーザー画面ブロックから呼ぶ。
    """
    st.header("📩 Ys Consulting Officeに個人相談")
    st.caption("ご相談はこちらから送信できます。管理側が確認し、折り返し返信いたします。")

    # セッション管理
    if "ci_user_view" not in st.session_state:
        st.session_state["ci_user_view"] = "list"  # "list" / "new" / "detail"
    if "ci_selected_inquiry_id" not in st.session_state:
        st.session_state["ci_selected_inquiry_id"] = None

    view = st.session_state["ci_user_view"]

    # ── ナビゲーション ──────────────────────────────────────────
    col_nav1, col_nav2, col_nav3 = st.columns([2, 2, 6])
    with col_nav1:
        if st.button("📋 相談履歴", use_container_width=True,
                     key="ci_nav_list", type="secondary" if view != "list" else "primary"):
            st.session_state["ci_user_view"] = "list"
            st.session_state["ci_selected_inquiry_id"] = None
            st.rerun()
    with col_nav2:
        if st.button("➕ 個人相談を送る", use_container_width=True,
                     key="ci_nav_new", type="secondary" if view != "new" else "primary"):
            st.session_state["ci_user_view"] = "new"
            st.session_state["ci_selected_inquiry_id"] = None
            st.rerun()

    st.divider()

    # ── ビュー切り替え ───────────────────────────────────────────
    if st.session_state["ci_user_view"] == "new":
        _render_user_new_inquiry(db, uid, tenant_id, display_name)

    elif st.session_state["ci_user_view"] == "detail":
        inq_id = st.session_state.get("ci_selected_inquiry_id")
        if not inq_id:
            st.session_state["ci_user_view"] = "list"
            st.rerun()
        _render_user_inquiry_detail(db, uid, tenant_id, inq_id)

    else:
        _render_user_inquiry_list(db, uid, tenant_id)


def _render_user_inquiry_list(db, uid: str, tenant_id: str) -> None:
    """ユーザーの相談一覧を表示する。"""
    inquiries = list_user_consulting_inquiries(db, tenant_id, uid, limit=50)

    if not inquiries:
        st.info("まだ相談がありません。「個人相談を送る」から新しい相談を作成できます。")
        return

    st.markdown(f"**相談件数: {len(inquiries)} 件**")

    for inq in inquiries:
        row = build_inquiry_summary_row(inq)
        inq_id = row["inquiry_id"]
        unread_mark = "🔵 " if inq.get("unread_for_user") else ""
        with st.container(border=True):
            col_a, col_b = st.columns([8, 2])
            with col_a:
                st.markdown(
                    f"**{unread_mark}{row['タイトル']}**　"
                    + _status_badge(row["_status_key"]),
                    unsafe_allow_html=True,
                )
                st.caption(
                    f"カテゴリ: {row['カテゴリ'] or '—'}　｜　"
                    f"最終更新: {row['最終更新']}　｜　"
                    f"最終送信者: {row['最終送信者']}"
                )
            with col_b:
                if st.button("詳細を見る", key=f"ci_user_open_{inq_id}",
                             use_container_width=True):
                    st.session_state["ci_selected_inquiry_id"] = inq_id
                    st.session_state["ci_user_view"] = "detail"
                    # 既読処理
                    try:
                        mark_inquiry_read_for_user(db, inq_id)
                    except Exception:
                        pass
                    st.rerun()


def _render_user_new_inquiry(
    db, uid: str, tenant_id: str, display_name: str
) -> None:
    """新規相談作成フォームを表示する。"""
    st.subheader("新しい相談を送る")

    with st.form(key="ci_new_inquiry_form", clear_on_submit=True):
        title = st.text_input(
            "相談タイトル *",
            placeholder="例：新規事業の方向性について",
            max_chars=100,
        )
        category = st.selectbox("カテゴリ（任意）", [""] + CATEGORY_OPTIONS)
        body = st.text_area(
            "相談内容 *",
            height=200,
            placeholder="ご相談の内容を詳しくご記入ください。",
        )
        supplement = st.text_area(
            "補足情報（任意）",
            height=80,
            placeholder="補足があればご記入ください。",
        )

        submitted = st.form_submit_button(
            "📨 相談を送信する",
            use_container_width=True,
            type="primary",
        )

    if submitted:
        if not title.strip():
            st.error("相談タイトルを入力してください。")
            return
        if not body.strip():
            st.error("相談内容を入力してください。")
            return

        try:
            inq_id = create_consulting_inquiry(
                db=db,
                tenant_id=tenant_id,
                user_id=uid,
                user_display_name=display_name or uid,
                title=title.strip(),
                body=body.strip(),
                category=category or "",
                supplement=supplement.strip(),
            )
            st.success("✅ 相談を送信しました。管理側からの返信をお待ちください。")
            st.session_state["ci_selected_inquiry_id"] = inq_id
            st.session_state["ci_user_view"] = "detail"
            st.rerun()
        except Exception as e:
            st.error(f"送信に失敗しました: {e}")


def _render_user_inquiry_detail(
    db, uid: str, tenant_id: str, inquiry_id: str
) -> None:
    """ユーザー向け相談詳細（スレッド表示）。"""
    inq = get_consulting_inquiry(db, inquiry_id)
    if not inq:
        st.error("相談が見つかりませんでした。")
        st.session_state["ci_user_view"] = "list"
        return

    # 権限チェック（自分の相談のみ）
    if not can_view_inquiry(inq, uid, is_admin=False):
        st.error("この相談にアクセスする権限がありません。")
        st.session_state["ci_user_view"] = "list"
        return

    # 既読処理
    if inq.get("unread_for_user"):
        try:
            mark_inquiry_read_for_user(db, inquiry_id)
        except Exception:
            pass

    col_back, col_status = st.columns([3, 7])
    with col_back:
        if st.button("← 一覧に戻る", key="ci_user_back"):
            st.session_state["ci_user_view"] = "list"
            st.session_state["ci_selected_inquiry_id"] = None
            st.rerun()
    with col_status:
        status_key = inq.get("status", "new")
        st.markdown(
            f"**現在のステータス：** " + _status_badge(status_key),
            unsafe_allow_html=True,
        )

    st.subheader(inq.get("title", "（無題）"))
    st.caption(
        f"カテゴリ: {inq.get('category') or '—'}　｜　"
        f"作成日時: {_fmt_dt(inq.get('created_at'))}"
    )
    if inq.get("supplement"):
        with st.expander("補足情報", expanded=False):
            st.write(inq.get("supplement"))

    st.divider()
    st.markdown("#### 📨 メッセージ履歴")

    messages = list_consulting_inquiry_messages(
        db, inquiry_id, visible_to="user", limit=200
    )
    if not messages:
        st.info("メッセージがありません。")
    else:
        for msg in messages:
            sender = msg.get("sender_type", "user")
            is_me = (sender == "user")
            align = "right" if is_me else "left"
            bg = "#dbeafe" if is_me else "#f3f4f6"
            label = "あなた" if is_me else "📬 管理側からの返信"
            ts = _fmt_dt(msg.get("created_at"))
            body_text = msg.get("body", "")

            st.markdown(
                f'<div style="text-align:{align};margin:6px 0;">'
                f'<span style="font-size:0.72rem;color:#6b7280;">{label}　{ts}</span><br>'
                f'<span style="display:inline-block;background:{bg};padding:8px 14px;'
                f'border-radius:12px;max-width:80%;text-align:left;'
                f'white-space:pre-wrap;word-break:break-word;">{body_text}</span>'
                f'</div>',
                unsafe_allow_html=True,
            )

    # 追記フォーム（closed 以外）
    if inq.get("status") != "closed":
        st.divider()
        st.markdown("#### ✏️ 追記・返信")
        with st.form(key=f"ci_user_reply_form_{inquiry_id}", clear_on_submit=True):
            reply_body = st.text_area(
                "メッセージを入力",
                height=120,
                placeholder="追加のご相談・返信内容をご記入ください。",
            )
            reply_submitted = st.form_submit_button(
                "📨 送信", use_container_width=True, type="primary"
            )

        if reply_submitted:
            if not reply_body.strip():
                st.error("メッセージを入力してください。")
                return
            try:
                add_consulting_inquiry_message(
                    db=db,
                    inquiry_id=inquiry_id,
                    tenant_id=tenant_id,
                    sender_type="user",
                    sender_id=uid,
                    body=reply_body.strip(),
                )
                st.success("✅ 送信しました。")
                st.rerun()
            except Exception as e:
                st.error(f"送信に失敗しました: {e}")
    else:
        st.info("この相談は完了としてクローズされています。")


# ============================================================
# UI: 管理側
# ============================================================
def create_admin_initiated_inquiry(
    db,
    admin_uid: str,
    target_uid: str,
    target_display_name: str,
    tenant_id: str,
    title: str,
    body: str,
) -> str:
    """管理側から特定ユーザーへ新規スレッドを起こす。"""
    import uuid, datetime
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    inquiry_id = str(uuid.uuid4())
    db.collection("consulting_inquiries").document(inquiry_id).set({
        "inquiry_id":        inquiry_id,
        "tenant_id":         tenant_id,
        "user_id":           target_uid,
        "user_display_name": target_display_name,
        "title":             title,
        "status":            "new",
        "created_at":        now,
        "updated_at":        now,
        "last_sender_type":  "admin",
        "unread_for_admin":  False,
        "unread_for_user":   True,
        "admin_memo":        "",
        "assigned_admin_id": admin_uid,
        "initiated_by":      "admin",
    })
    message_id = str(uuid.uuid4())
    db.collection("consulting_inquiries").document(inquiry_id).collection("messages").document(message_id).set({
        "message_id":      message_id,
        "inquiry_id":      inquiry_id,
        "tenant_id":       tenant_id,
        "sender_type":     "admin",
        "sender_id":       admin_uid,
        "body":            body,
        "created_at":      now,
        "visible_to_admin": True,
        "visible_to_user":  True,
    })
    return inquiry_id


def render_admin_consulting_inquiry_page(
    db,
    tenant_id: str,
    admin_uid: str = "admin",
) -> None:
    """
    管理側個人相談管理ページ全体を描画する。
    app.py の admin_menu 分岐から呼ぶ。
    """
    # ── バッジ（新着・未読件数）──────────────────────────────
    try:
        all_inq = list_admin_consulting_inquiries(db, tenant_id, limit=200)
        cnt_new    = sum(1 for i in all_inq if i.get("status") == "new")
        cnt_unread = sum(1 for i in all_inq if i.get("unread_for_admin"))
    except Exception:
        all_inq    = []
        cnt_new    = 0
        cnt_unread = 0

    badge_parts = []
    if cnt_unread:
        badge_parts.append(
            f'<span style="background:#ef4444;color:#fff;padding:2px 10px;'
            f'border-radius:12px;font-size:0.78rem;font-weight:700;">'
            f'🔴 未読 {cnt_unread}件</span>'
        )
    if cnt_new:
        badge_parts.append(
            f'<span style="background:#f59e0b;color:#fff;padding:2px 10px;'
            f'border-radius:12px;font-size:0.78rem;font-weight:700;">'
            f'⚡ 新規 {cnt_new}件</span>'
        )
    header_html = "　".join(badge_parts) if badge_parts else '<span style="color:#6b7280;font-size:0.78rem;">新着なし</span>'
    st.header("💬 個人相談管理")
    st.markdown(header_html, unsafe_allow_html=True)

    # ── 管理から新規スレッド作成 ─────────────────────────────
    with st.expander("📤 管理から新規スレッドを起こす（全ユーザー対象）", expanded=False):
        st.caption("personal_consulting 未開放ユーザーへも送信可能です。")
        try:
            _all_users = list(db.collection("users").limit(500).stream())
            _user_opts = []
            for _u in _all_users:
                _ud = _u.to_dict() or {}
                _uid_val = _ud.get("uid") or _u.id
                _dname = _ud.get("display_name") or ""
                _user_opts.append({"uid": _uid_val, "label": f"{_uid_val}　{_dname}"})
            _user_opts.sort(key=lambda x: x["uid"])
        except Exception as _ue:
            _user_opts = []
            st.error(f"ユーザー取得失敗: {_ue}")

        if _user_opts:
            with st.form(key="ci_admin_new_thread_form", clear_on_submit=True):
                _sel_label = st.selectbox(
                    "送信先ユーザー",
                    [x["label"] for x in _user_opts],
                    key="ci_admin_new_target_sel",
                )
                _new_title = st.text_input(
                    "タイトル",
                    placeholder="例：プランのご案内",
                    key="ci_admin_new_title",
                )
                _new_body = st.text_area(
                    "本文",
                    height=120,
                    placeholder="ご連絡内容をご記入ください",
                    key="ci_admin_new_body",
                )
                _new_submit = st.form_submit_button("📤 送信", use_container_width=True, type="primary")

            if _new_submit:
                if not _new_title.strip() or not _new_body.strip():
                    st.error("タイトルと本文は必須です。")
                else:
                    _target = next((x for x in _user_opts if x["label"] == _sel_label), None)
                    if _target:
                        try:
                            _new_inq_id = create_admin_initiated_inquiry(
                                db=db,
                                admin_uid=admin_uid,
                                target_uid=_target["uid"],
                                target_display_name=_target["label"],
                                tenant_id=tenant_id,
                                title=_new_title.strip(),
                                body=_new_body.strip(),
                            )
                            st.success(f"✅ {_target['uid']} にスレッドを作成しました。（ID: {_new_inq_id[:8]}...）")
                            st.rerun()
                        except Exception as _nie:
                            st.error(f"作成失敗: {_nie}")
        else:
            st.info("ユーザーが存在しません。")

    st.divider()

    # ── セッション管理 ────────────────────────────────────────
    if "ci_admin_view" not in st.session_state:
        st.session_state["ci_admin_view"] = "list"
    if "ci_admin_selected_id" not in st.session_state:
        st.session_state["ci_admin_selected_id"] = None

    view = st.session_state["ci_admin_view"]

    if view == "detail":
        inq_id = st.session_state.get("ci_admin_selected_id")
        if not inq_id:
            st.session_state["ci_admin_view"] = "list"
            st.rerun()
        _render_admin_inquiry_detail(db, tenant_id, inq_id, admin_uid)
    else:
        _render_admin_inquiry_list(db, tenant_id)


def _render_admin_inquiry_list(db, tenant_id: str) -> None:
    """管理側: 相談一覧を表示する。"""
    # ── 絞り込み ────────────────────────────────────────────
    col_f1, col_f2, col_f3 = st.columns([3, 2, 5])
    with col_f1:
        status_options = ["すべて"] + list(INQUIRY_STATUSES.keys())
        status_labels  = {"すべて": "すべて"} | INQUIRY_STATUSES
        status_sel = st.selectbox(
            "ステータス絞り込み",
            status_options,
            format_func=lambda x: status_labels.get(x, x),
            key="ci_admin_status_filter",
        )
    with col_f2:
        unread_only = st.checkbox("未読のみ", key="ci_admin_unread_filter")
    with col_f3:
        kw = st.text_input(
            "キーワード検索（タイトル / ユーザーID）",
            key="ci_admin_kw_filter",
            placeholder="例：山田, 戦略",
        )

    # ── データ取得 ────────────────────────────────────────────
    inquiries = list_admin_consulting_inquiries(
        db=db,
        tenant_id=tenant_id,
        status_filter=status_sel if status_sel != "すべて" else None,
        unread_only=unread_only,
        search_keyword=kw,
        limit=100,
    )

    # ── ステータス別集計 ─────────────────────────────────────
    all_inq = list_admin_consulting_inquiries(db, tenant_id, limit=200)
    stat_counts = {}
    for s_key, s_label in INQUIRY_STATUSES.items():
        stat_counts[s_label] = sum(1 for i in all_inq if i.get("status") == s_key)
    cols_stat = st.columns(len(stat_counts))
    for i, (label, count) in enumerate(stat_counts.items()):
        with cols_stat[i]:
            st.metric(label, count)

    st.divider()
    st.markdown(f"**表示件数: {len(inquiries)} 件**")

    if not inquiries:
        st.info("該当する相談がありません。")
        return

    for inq in inquiries:
        row = build_inquiry_summary_row(inq)
        inq_id = row["inquiry_id"]
        unread_mark = "🔴 " if inq.get("unread_for_admin") else ""

        with st.container(border=True):
            col_a, col_b = st.columns([8, 2])
            with col_a:
                st.markdown(
                    f"**{unread_mark}{row['タイトル']}**　"
                    + _status_badge(row["_status_key"]),
                    unsafe_allow_html=True,
                )
                st.caption(
                    f"ユーザー: {row['表示名']} ({row['ユーザーID']})　｜　"
                    f"カテゴリ: {row['カテゴリ'] or '—'}　｜　"
                    f"最終更新: {row['最終更新']}　｜　"
                    f"最終送信者: {row['最終送信者']}"
                )
            with col_b:
                if st.button("詳細・返信", key=f"ci_admin_open_{inq_id}",
                             use_container_width=True, type="primary"):
                    st.session_state["ci_admin_selected_id"] = inq_id
                    st.session_state["ci_admin_view"] = "detail"
                    try:
                        mark_inquiry_read_for_admin(db, inq_id)
                    except Exception:
                        pass
                    st.rerun()


def _render_admin_inquiry_detail(
    db, tenant_id: str, inquiry_id: str, admin_uid: str
) -> None:
    """管理側: 相談詳細ページ（返信・ステータス変更・メモ）。"""
    inq = get_consulting_inquiry(db, inquiry_id)
    if not inq:
        st.error("相談が見つかりませんでした。")
        st.session_state["ci_admin_view"] = "list"
        return

    if st.button("← 一覧に戻る", key="ci_admin_back"):
        st.session_state["ci_admin_view"] = "list"
        st.session_state["ci_admin_selected_id"] = None
        st.rerun()

    st.subheader(inq.get("title", "（無題）"))

    # ── メタ情報 ──────────────────────────────────────────────
    info_col1, info_col2, info_col3 = st.columns(3)
    with info_col1:
        st.markdown(
            f"**ステータス** " + _status_badge(inq.get("status", "new")),
            unsafe_allow_html=True,
        )
    with info_col2:
        st.markdown(f"**ユーザー:** {inq.get('user_display_name','')} ({inq.get('user_id','')})")
    with info_col3:
        st.markdown(f"**カテゴリ:** {inq.get('category') or '—'}")

    st.caption(
        f"作成: {_fmt_dt(inq.get('created_at'))}　｜　"
        f"最終更新: {_fmt_dt(inq.get('last_message_at'))}"
    )
    if inq.get("supplement"):
        with st.expander("補足情報", expanded=False):
            st.write(inq.get("supplement"))

    st.divider()

    # ── メッセージスレッド ────────────────────────────────────
    st.markdown("#### 📨 メッセージ履歴")
    messages = list_consulting_inquiry_messages(
        db, inquiry_id, visible_to="admin", limit=200
    )
    if not messages:
        st.info("メッセージがありません。")
    else:
        for msg in messages:
            sender = msg.get("sender_type", "user")
            is_admin = (sender == "admin")
            align = "right" if is_admin else "left"
            bg = "#dcfce7" if is_admin else "#f3f4f6"
            label = "管理側（あなた）" if is_admin else f"👤 {inq.get('user_display_name', 'ユーザー')}"
            ts = _fmt_dt(msg.get("created_at"))
            body_text = msg.get("body", "")

            st.markdown(
                f'<div style="text-align:{align};margin:6px 0;">'
                f'<span style="font-size:0.72rem;color:#6b7280;">{label}　{ts}</span><br>'
                f'<span style="display:inline-block;background:{bg};padding:8px 14px;'
                f'border-radius:12px;max-width:80%;text-align:left;'
                f'white-space:pre-wrap;word-break:break-word;">{body_text}</span>'
                f'</div>',
                unsafe_allow_html=True,
            )

    st.divider()

    # ── 管理側返信フォーム ────────────────────────────────────
    st.markdown("#### ✏️ 返信する")
    with st.form(key=f"ci_admin_reply_form_{inquiry_id}", clear_on_submit=True):
        reply_body = st.text_area(
            "返信内容",
            height=150,
            placeholder="ユーザーへの返信内容を入力してください。",
        )
        reply_submitted = st.form_submit_button(
            "📨 返信を送信", use_container_width=True, type="primary"
        )

    if reply_submitted:
        if not reply_body.strip():
            st.error("返信内容を入力してください。")
        else:
            try:
                add_consulting_inquiry_message(
                    db=db,
                    inquiry_id=inquiry_id,
                    tenant_id=tenant_id,
                    sender_type="admin",
                    sender_id=admin_uid,
                    body=reply_body.strip(),
                )
                st.success("✅ 返信を送信しました。")
                st.rerun()
            except Exception as e:
                st.error(f"返信送信に失敗しました: {e}")

    st.divider()

    # ── ステータス変更 / 担当メモ ─────────────────────────────
    st.markdown("#### ⚙️ 管理操作")
    mgmt_col1, mgmt_col2 = st.columns(2)

    with mgmt_col1:
        st.markdown("**ステータス変更**")
        status_keys   = list(INQUIRY_STATUSES.keys())
        current_status = inq.get("status", "new")
        new_status = st.selectbox(
            "新しいステータス",
            status_keys,
            index=status_keys.index(current_status) if current_status in status_keys else 0,
            format_func=lambda x: INQUIRY_STATUSES.get(x, x),
            key=f"ci_admin_status_sel_{inquiry_id}",
        )
        if st.button("🔄 ステータスを更新", key=f"ci_admin_status_btn_{inquiry_id}",
                     use_container_width=True):
            try:
                update_consulting_inquiry_status(db, inquiry_id, new_status)
                st.success(f"✅ ステータスを「{INQUIRY_STATUSES.get(new_status, new_status)}」に更新しました。")
                st.rerun()
            except Exception as e:
                st.error(f"更新に失敗しました: {e}")

    with mgmt_col2:
        st.markdown("**担当メモ（内部用）**")
        current_memo = inq.get("admin_memo", "")
        new_memo = st.text_area(
            "管理メモ",
            value=current_memo,
            height=100,
            placeholder="担当者向けの内部メモ",
            key=f"ci_admin_memo_{inquiry_id}",
        )
        if st.button("💾 メモを保存", key=f"ci_admin_memo_btn_{inquiry_id}",
                     use_container_width=True):
            try:
                update_consulting_admin_memo(db, inquiry_id, new_memo)
                st.success("✅ メモを保存しました。")
                st.rerun()
            except Exception as e:
                st.error(f"保存に失敗しました: {e}")

    # ── 担当者設定 ────────────────────────────────────────────
    st.markdown("**担当者 (assigned_admin_id)**")
    current_assigned = inq.get("assigned_admin_id", "")
    new_assigned = st.text_input(
        "担当管理者ID",
        value=current_assigned,
        key=f"ci_admin_assign_{inquiry_id}",
        placeholder="admin",
    )
    if st.button("💾 担当者を保存", key=f"ci_admin_assign_btn_{inquiry_id}"):
        try:
            update_consulting_assigned_admin(db, inquiry_id, new_assigned)
            st.success("✅ 担当者を保存しました。")
            st.rerun()
        except Exception as e:
            st.error(f"保存に失敗しました: {e}")

