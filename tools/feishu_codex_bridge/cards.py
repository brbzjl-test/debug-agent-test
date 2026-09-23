"""Card 2.0 templates: one primary action, explicit scoped callback tokens."""


def card(title, description, buttons, *, color="blue", url=None, url_label="打开问题台账", selection=None):
    elements = [{"tag": "markdown", "content": description}]
    if selection:
        token, options = selection
        elements.append({
            "tag": "select_static", "width": "fill",
            "placeholder": {"tag": "plain_text", "content": "选择旧问题 ID，创建本次 sub-ID"},
            "options": [{"text": {"tag": "plain_text", "content": label}, "value": value}
                        for label, value in options],
            "behaviors": [{"type": "callback", "value": {"bridge_action": token}}],
        })
    for index, (label, token) in enumerate(buttons):
        elements.append({
            "tag": "button", "text": {"tag": "plain_text", "content": label},
            "type": "primary_filled" if index == 0 else "default", "width": "fill",
            "behaviors": [{"type": "callback", "value": {"bridge_action": token}}],
        })
    if url:
        elements.append({
            "tag": "button", "text": {"tag": "plain_text", "content": url_label},
            "type": "default", "width": "fill",
            "behaviors": [{"type": "open_url", "default_url": url}],
        })
    return {
        "schema": "2.0", "config": {"width_mode": "default", "enable_forward": False},
        "header": {"title": {"tag": "plain_text", "content": title}, "template": color,
                   "icon": {"tag": "standard_icon", "token": "todo_colorful"}},
        "body": {"direction": "vertical", "vertical_spacing": "8px", "elements": elements},
    }
