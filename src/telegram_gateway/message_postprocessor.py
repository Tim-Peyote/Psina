"""
Message Post-Processor — умная предобработка ответов перед отправкой.

Берёт "сырой" текст (от LLM или хардкода) и приводит его к красивому HTML-виду:
- Нормализует отступы и переносы строк
- Конвертирует markdown-стиль в HTML (**жирный**, *курсив*, `код`)
- Форматирует списки с единообразными буллетами
- Превращает голые URL в кликабельные ссылки
- Конвертирует @упоминания в HTML-ментшены
- Умно разбивает длинные сообщения
"""

import re
from typing import Optional


class MessagePostProcessor:
    """
    Постпроцессор сообщений.

    Пайплайн:
    1. normalize_whitespace
    2. convert_markdown_to_html
    3. format_lists
    4. format_urls
    5. finalize
    """

    # Максимальная длина одного сообщения в Telegram
    MAX_MESSAGE_LENGTH = 4096

    # Порог для умной разбивки
    SPLIT_THRESHOLD = 1000

    def process(self, text: str) -> str:
        """
        Главный метод — запускает весь пайплайн обработки.

        Args:
            text: сырой текст от LLM или хардкода

        Returns:
            Красивый HTML-текст, готовый к отправке
        """
        if not text or not text.strip():
            return text

        result = text
        result = self._normalize_whitespace(result)
        result = self._convert_markdown_to_html(result)
        result = self._format_lists(result)
        result = self._format_urls(result)
        result = self._finalize(result)
        return result

    def split_message(self, text: str) -> list[str]:
        """
        Умно разбивает длинное сообщение на части.

        Разбивает по логическим границам (пустые строки, заголовки, списки),
        а не тупо по символу.

        Args:
            text: обработанный HTML-текст

        Returns:
            Список частей, каждая <= MAX_MESSAGE_LENGTH
        """
        if len(text) <= self.MAX_MESSAGE_LENGTH:
            return [text]

        parts = []
        current = ""

        # Разбиваем по двойным переносам (логические блоки)
        blocks = re.split(r'\n\n+', text)

        for block in blocks:
            block = block.strip()
            if not block:
                continue

            # Если блок + текущий > лимит — сохраняем текущий
            if current and len(current) + len(block) + 2 > self.MAX_MESSAGE_LENGTH:
                parts.append(current.strip())
                current = ""

            # Если单个 блок > лимит — разбиваем его
            if len(block) > self.MAX_MESSAGE_LENGTH:
                if current:
                    parts.append(current.strip())
                    current = ""
                parts.extend(self._split_block_hard(block))
            else:
                current = (current + "\n\n" + block) if current else block

        if current.strip():
            parts.append(current.strip())

        return parts if parts else [text]

    def _normalize_whitespace(self, text: str) -> str:
        """
        Нормализует отступы и переносы строк.

        - Убирает trailing пробелы
        - Схлопывает 3+ переноса до 2
        - Убирает пустые строки в начале/конце
        - Нормализует mix of \r\n and \n
        """
        # Нормализуем line endings
        text = text.replace('\r\n', '\n').replace('\r', '\n')

        # Убираем trailing spaces на каждой строке
        lines = [line.rstrip() for line in text.split('\n')]
        text = '\n'.join(lines)

        # Схлопываем 3+ переноса до 2
        text = re.sub(r'\n{3,}', '\n\n', text)

        # Убираем пустые строки в начале/конце
        text = text.strip()

        return text

    def _convert_markdown_to_html(self, text: str) -> str:
        """
        Конвертирует markdown-стиль разметки в HTML теги.

        Поддерживает:
        - **жирный** или __жирный__ → <b>жирный</b>
        - *курсив* или _курсив_ → <i>курсив</i>
        - ~~зачёркнутый~~ → <s>зачёркнутый</s>
        - `код` → <code>код</code>
        - ```блок кода``` → <pre>блок кода</pre>

        Важно: порядок имеет значение — сначала block code, потом inline.
        """
        # 1. Блоки кода (``` ... ```)
        text = self._convert_code_blocks(text)

        # 2. Inline код (`...`) — делаем до жирного/курсива
        text = self._convert_inline_code(text)

        # 3. Жирный (**...** или __...__)
        text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text, flags=re.DOTALL)
        text = re.sub(r'__(.+?)__', r'<b>\1</b>', text, flags=re.DOTALL)

        # 4. Курсив (*...* или _..._)
        # _текст_ только если не часть слова (чтобы не ломать snake_case)
        text = re.sub(r'\*(.+?)\*', r'<i>\1</i>', text, flags=re.DOTALL)
        text = re.sub(r'(?<!\w)_(.+?)_(?!\w)', r'<i>\1</i>', text, flags=re.DOTALL)

        # 5. Зачёркнутый (~~...~~)
        text = re.sub(r'~~(.+?)~~', r'<s>\1</s>', text, flags=re.DOTALL)

        return text

    def _convert_code_blocks(self, text: str) -> str:
        """Конвертирует ```code block``` в <pre>...</pre>."""
        def replace_code_block(match):
            code = match.group(1).strip()
            # Убираем язык если указан (```python, ```js и т.д.)
            lang_match = re.match(r'^\w+\n', code)
            if lang_match:
                code = code[lang_match.end():]
            return f'<pre>{code}</pre>'

        text = re.sub(r'```(?:\w*)\n(.*?)```', replace_code_block, text, flags=re.DOTALL)
        return text

    def _convert_inline_code(self, text: str) -> str:
        """Конвертирует `код` в <code>код</code>."""
        # Не трогаем если уже внутри <pre>
        parts = re.split(r'(<pre>.*?</pre>)', text, flags=re.DOTALL)
        result = []
        for part in parts:
            if part.startswith('<pre>'):
                result.append(part)
            else:
                part = re.sub(r'`([^`]+)`', r'<code>\1</code>', part)
                result.append(part)
        return ''.join(result)

    def _format_lists(self, text: str) -> str:
        """
        Форматирует списки с единообразными буллетами.

        - `•`, `▸`, `-`, `*` в начале строки → `•`
        - Нумерованные списки `1.`, `2.` остаются как есть
        - Вложенные списки (с отступом) → `  ▸`
        """
        lines = text.split('\n')
        result = []

        for line in lines:
            stripped = line.lstrip()
            indent = len(line) - len(stripped)

            # Маркированные списки
            if re.match(r'^[•▸\-\*]\s+', stripped):
                content = re.sub(r'^[•▸\-\*]\s+', '', stripped)
                if indent > 0:
                    result.append(f'{" " * indent}▸ {content}')
                else:
                    result.append(f'• {content}')
            # Нумерованные списки — оставляем как есть
            else:
                result.append(line)

        return '\n'.join(result)

    def _format_urls(self, text: str) -> str:
        """
        Превращает голые URL в HTML-ссылки.

        - http://example.com → <a href="http://example.com">http://example.com</a>
        - [текст](url) → <a href="url">текст</a>
        - Не трогаем если URL уже внутри <a href>
        """
        # 1. Markdown-стиль ссылок [текст](url) → HTML
        def replace_md_link(match):
            link_text = match.group(1)
            url = match.group(2)
            return f'<a href="{url}">{link_text}</a>'

        # Не трогаем если уже HTML
        parts = re.split(r'(<a\s[^>]+>.*?</a>)', text, flags=re.DOTALL)
        result = []

        for part in parts:
            if part.startswith('<a '):
                result.append(part)
            else:
                part = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', replace_md_link, part)
                result.append(part)

        text = ''.join(result)

        # 2. Голые URL → кликабельные
        url_pattern = r'(?<!href=")(?<!<a href=")(https?://[^\s<>\)\]]+)'

        def replace_bare_url(match):
            url = match.group(0)
            # Короткие URL показываем полностью, длинные — обрезаем
            display = url
            if len(display) > 50:
                display = display[:47] + '…'
            return f'<a href="{url}">{display}</a>'

        # Применяем только к частям вне HTML тегов
        parts = re.split(r'(<[^>]+>)', text)
        result = []
        for part in parts:
            if part.startswith('<') and part.endswith('>'):
                result.append(part)
            else:
                part = re.sub(url_pattern, replace_bare_url, part)
                result.append(part)

        return ''.join(result)

    def _finalize(self, text: str) -> str:
        """
        Финальная обработка — чистим артефакты.

        - Убираем двойные пробелы
        - Фиксим переносы после тегов
        - Убираем пустые теги
        """
        # Двойные пробелы → один
        text = re.sub(r' {2,}', ' ', text)

        # Переносы после закрывающих тегов
        text = re.sub(r'(</[a-z]+>)\n{2,}(<[a-z]+>)', r'\1\n\n\2', text)

        # Пустые теги <b></b>, <i></i>
        text = re.sub(r'<(b|i|code|s)>\s*</\1>', '', text)

        return text.strip()

    def _split_block_hard(self, block: str) -> list[str]:
        """
        Жёсткая разбивка блока > MAX_MESSAGE_LENGTH.

        Режем по предложениям, чтобы не ломать HTML теги.
        """
        parts = []
        current = ""

        # Разбиваем по предложениям (. ! ?)
        sentences = re.split(r'(?<=[.!?])\s+', block)

        for sentence in sentences:
            if len(current) + len(sentence) + 1 <= self.MAX_MESSAGE_LENGTH:
                current = (current + ' ' + sentence).strip()
            else:
                if current:
                    parts.append(current)
                # Если одно предложение > лимит — режем тупо
                if len(sentence) > self.MAX_MESSAGE_LENGTH:
                    for i in range(0, len(sentence), self.MAX_MESSAGE_LENGTH):
                        parts.append(sentence[i:i + self.MAX_MESSAGE_LENGTH])
                    current = ""
                else:
                    current = sentence

        if current:
            parts.append(current)

        return parts


# Singleton
message_postprocessor = MessagePostProcessor()
