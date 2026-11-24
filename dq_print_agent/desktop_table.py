"""Paint table cells over a native Treeview, retaining its selection and keyboard conventions."""
import tkinter as tk
from tkinter import font as tkfont, ttk

from .desktop_theme import BLUE, INK


class ClientTable(ttk.Treeview):
    def __init__(self, parent, *, paint_cell=None, **kwargs):
        super().__init__(parent, **kwargs)
        self.paint_cell = paint_cell
        style = ttk.Style(self)
        self.text_font = tkfont.Font(self, font=style.lookup(self.cget('style') or 'Treeview', 'font') or 'TkDefaultFont')
        self.pad = max(6, self.text_font.measure('中') // 2)
        self.rows_canvas = tk.Canvas(self, highlightthickness=0, bd=0, bg='white', takefocus=False)
        self.rows_canvas.bind('<Button-1>', self._click)
        self.rows_canvas.bind('<Double-1>', self._activate)
        self.rows_canvas.bind('<MouseWheel>', self._wheel)
        self.bind('<Return>', self._activate)
        self._paint_ticket = None
        for event in ('<Map>', '<Configure>', '<Expose>', '<B1-Motion>', '<<TreeviewSelect>>'):
            self.bind(event, self.redraw, add='+')

    def _click(self, event):
        self.focus_set()
        row = self.identify_row(event.y + self.rows_canvas.winfo_y())
        mode = str(self.cget('selectmode'))
        if row and mode != 'none':
            if mode == 'extended' and event.state & 1 and self.focus():
                first, last = sorted((self.index(self.focus()), self.index(row)))
                self.selection_set(self.get_children()[first:last + 1])
            elif mode == 'extended' and event.state & 4:
                self.selection_toggle(row)
            else:
                self.selection_set(row)
            self.focus(row)
        return 'break'

    def _activate(self, event):
        if event.widget is self.rows_canvas:
            y = event.y + self.rows_canvas.winfo_y()
        else:
            current = self.focus()
            box = self.bbox(current) if current else ()
            if not box:
                return 'break'
            y = box[1] + box[3] // 2
        if self.identify_row(y):
            self.event_generate('<<TableActivate>>', y=y)
        return 'break'

    def _wheel(self, event):
        self.event_generate('<MouseWheel>', delta=event.delta, state=event.state)
        return 'break'

    def configure(self, cnf=None, **kwargs):
        # tk's class bindings also scroll internally (arrow keys, Page Down);
        # redraw after those movements as well as scrollbar thumb movements.
        for key in ('xscrollcommand', 'yscrollcommand'):
            command = kwargs.get(key)
            if callable(command):
                def scrolled(first, last, callback=command):
                    callback(first, last)
                    self.redraw()
                kwargs[key] = scrolled
        result = super().configure(cnf, **kwargs)
        if kwargs and hasattr(self, '_paint_ticket'):
            self.redraw()
        return result

    def redraw(self, _event=None):
        if self._paint_ticket is None:
            self._paint_ticket = self.after_idle(self._paint)

    def _paint(self):
        self._paint_ticket = None
        canvas = self.rows_canvas
        canvas.delete('all')
        if not self.winfo_ismapped():
            return
        rows = [(key, self.bbox(key)) for key in self.get_children()]
        visible = [(key, box) for key, box in rows if box]
        if not visible:
            canvas.place_forget()
            return
        display = self.cget('displaycolumns')
        columns = self.cget('columns') if display == ('#all',) else display
        top = visible[0][1][1]
        width = self.winfo_width()
        bottom = min(self.winfo_height(), visible[-1][1][1] + visible[-1][1][3])
        if bottom <= top:
            canvas.place_forget()
            return
        canvas.place(x=0, y=top, width=width, height=bottom-top)
        canvas.tk.call('lower', canvas._w)  # Native action buttons stay above row paint.
        selected = self.selection()
        for key, (_, row_y, _, height) in visible:
            y = row_y - top
            active = key in selected
            if active:
                canvas.create_rectangle(0, y, width, y + height, fill='#e5efff', outline='')
            for column in columns:
                cell = self.bbox(key, column)
                if not cell or cell[0] + cell[2] <= 0 or cell[0] >= width:
                    continue
                x, _, cell_width, _ = cell
                value = self.set(key, column)
                if self.paint_cell and self.paint_cell(self, canvas, key, column, value,
                                                       x, y, cell_width, height, active):
                    continue
                text = self._fit_text(value, cell_width - 2 * self.pad)
                canvas.create_text(x + self.pad, y + height // 2, anchor='w',
                                   text=text, font=self.text_font, fill=INK)
            canvas.create_line(0, y + height - 1, width, y + height - 1, fill='#e0e6ee')
            if active:
                canvas.create_rectangle(0, y, max(3, self.pad // 3), y + height, fill=BLUE, outline='')

    def _fit_text(self, value, width):
        if self.text_font.measure(value) <= width:
            return value
        low, high = 0, len(value)
        while low < high:
            mid = (low + high + 1) // 2
            if self.text_font.measure(value[:mid] + '…') <= width:
                low = mid
            else:
                high = mid - 1
        return value[:low] + '…' if width >= self.text_font.measure('…') else ''

    def insert(self, *args, **kwargs):
        result = super().insert(*args, **kwargs)
        self.redraw()
        return result

    def delete(self, *items):
        super().delete(*items)
        self.redraw()

    def item(self, item, option=None, **kwargs):
        result = super().item(item, option, **kwargs)
        if kwargs:
            self.redraw()
        return result

    def move(self, *args):
        super().move(*args)
        self.redraw()

    def destroy(self):
        if self._paint_ticket is not None:
            self.after_cancel(self._paint_ticket)
            self._paint_ticket = None
        super().destroy()
