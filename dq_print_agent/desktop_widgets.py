"""Shared controls for the print client's Windows desktop interface."""

from tkinter import ttk


def _scroll_style(master, orient):
    style = ttk.Style(master)
    direction = orient.title()
    thickness = max(6, round(master.winfo_fpixels('6p')))
    name = f'Auto{thickness}.{direction}.TScrollbar'
    trough = f'Auto{direction}.trough'
    thumb = f'Auto{direction}.thumb'
    if trough not in style.element_names():
        for element, part in ((trough, 'trough'), (thumb, 'thumb')):
            style.element_create(element, 'from', 'clam', f'{direction}.Scrollbar.{part}')
    style.layout(name, [(trough, {'sticky': 'nsew', 'children': [(thumb, {'sticky': 'nsew'})]})])
    track = '#edf1f5'
    handle = '#b7c3d1'
    style.configure(name, arrowsize=thickness, sliderlength=round(thickness * 2.5),
                    gripcount=0, troughcolor=track, bordercolor=track,
                    background=handle, lightcolor=handle, darkcolor=handle)
    hover = [('pressed', '#566b83'), ('active', '#72869d')]
    style.map(name, background=hover, lightcolor=hover, darkcolor=hover)
    return name


class AutoScrollbar(ttk.Scrollbar):
    """Connect set to xscrollcommand or yscrollcommand; use pack or grid as usual."""

    def __init__(self, master, **kwargs):
        if 'style' not in kwargs:
            kwargs['style'] = _scroll_style(master, str(kwargs.get('orient', 'vertical')))
        super().__init__(master, **kwargs)
        self._hidden_by = None
        self._pack_options = {}
        self._pack_after = []
        self._needs_scroll = True
        self._pending_layout = None
        self._destroyed = False
        self.bind('<Destroy>', self._cancel_layout, add='+')

    def set(self, first, last):
        if self._destroyed:
            return
        super().set(first, last)
        self._needs_scroll = float(first) > 0 or float(last) < 1
        visible = self._hidden_by is None
        if self._pending_layout is None and self._needs_scroll != visible:
            # Coalesce viewport updates before changing the parent's geometry.
            self._pending_layout = self.after_idle(self._update_visibility)

    def _update_visibility(self):
        self._pending_layout = None
        if self._needs_scroll and self._hidden_by == 'grid':
            self.grid()  # grid_remove preserves the cell and all its options.
            self._hidden_by = None
        elif self._needs_scroll and self._hidden_by == 'pack':
            options = dict(self._pack_options)
            # pack_forget loses the ordering. Reinsert before the next surviving
            # sibling so an expanding canvas cannot claim the scrollbar's space.
            packed = options['in'].pack_slaves()
            for sibling in self._pack_after:
                if sibling in packed:
                    options['before'] = sibling
                    break
            self.pack(**options)
            self._hidden_by = None
        elif not self._needs_scroll and self._hidden_by is None:
            manager = self.winfo_manager()
            if manager == 'grid':
                self.grid_remove()
                self._hidden_by = manager
            elif manager == 'pack':
                self._pack_options = self.pack_info()
                parent = self._pack_options['in']
                siblings = parent.pack_slaves()
                # Remember the full packing order while peer scrollbars are
                # hidden. Either bar may come back first after a resize.
                order = getattr(parent, '_auto_scroll_pack_order', ())
                if [item for item in order if item in siblings] == siblings:
                    siblings = order
                else:
                    parent._auto_scroll_pack_order = siblings
                self._pack_after = siblings[siblings.index(self) + 1:]
                self.pack_forget()
                self._hidden_by = manager

    def _cancel_layout(self, _event):
        # A Tk window may be destroyed without going through Python's destroy().
        self._destroyed = True
        if self._pending_layout is not None:
            self.after_cancel(self._pending_layout)
            self._pending_layout = None
