"""DPI-aware ttk surfaces and line icons for the Windows print client."""

import math
import tkinter as tk
from tkinter import ttk

from PIL import Image, ImageDraw, ImageTk


BLUE = '#1556ed'
BG = '#eff5ff'
WHITE = '#ffffff'
BORDER = '#d0d9e8'
INK = '#111827'
MUTED = '#536176'
NAVY = '#0d1c35'
FONT = 'Microsoft YaHei UI'


def _px(root, logical):
    return max(1, round(logical * root.winfo_fpixels('1i') / 96))


def _images(root):
    if not hasattr(root, '_print_theme_images'):
        root._print_theme_images = {}
    return root._print_theme_images


def _surface(root, radius, fill, outline, outer):
    cache = _images(root)
    edge = _px(root, radius)
    stroke = _px(root, 1)
    key = ('surface', edge, stroke, fill, outline, outer)
    if key not in cache:
        # Tk tiles the middle, including on Win32. Use a sizeable solid tile
        # so a wide panel redraw needs only a handful of bitmap blits per row.
        size = 2 * (edge + 2) + _px(root, 96)
        high = size * 4
        image = Image.new('RGBA', (high, high), outer or (0, 0, 0, 0))
        ImageDraw.Draw(image).rounded_rectangle(
            (0, 0, high - 1, high - 1), radius=edge * 4,
            fill=fill, outline=outline, width=stroke * 4,
        )
        cache[key] = ImageTk.PhotoImage(image.resize((size, size), Image.Resampling.LANCZOS), master=root)
    return cache[key]


def icon(name, size=20, color=INK, master=None):
    """Return a cached transparent line icon. Size is in 96-DPI design pixels."""
    master = master if master is not None else tk._default_root
    if master is None:
        raise RuntimeError('Create a Tk window before requesting a print-client icon.')
    root = master._root()
    pixels = _px(root, size)
    cache = _images(root)
    key = ('icon', name, pixels, color)
    if key in cache:
        return cache[key]
    # Draw on a 24-unit grid with rounded line caps. Supersampling also keeps
    # diagonal strokes smooth when an icon is displayed at 125% Windows DPI.
    unit = max(6, math.ceil(pixels * 4 / 24))
    image = Image.new('RGBA', (24 * unit, 24 * unit))
    pen = ImageDraw.Draw(image)
    width = round(1.65 * unit)

    def box(coords):
        return tuple(round(v * unit) for v in coords)

    def dot(x, y, radius):
        pen.ellipse(box((x - radius, y - radius, x + radius, y + radius)), fill=color)

    def line(*points):
        pen.line([box(p) for p in points], fill=color, width=width, joint='curve')
        for x, y in (points[0], points[-1]):
            dot(x, y, .78)

    def rect(coords, radius=1.5):
        pen.rounded_rectangle(box(coords), radius=round(radius * unit), outline=color, width=width)

    def circle(coords):
        pen.ellipse(box(coords), outline=color, width=width)

    if name == 'printer':
        line((6.5, 7.7), (6.5, 3.3), (17.5, 3.3), (17.5, 7.7))
        line((6.4, 16.7), (2.8, 16.7), (2.8, 9.3), (4.5, 7.6),
             (19.6, 7.6), (21.2, 9.3), (21.2, 16.7), (17.6, 16.7))
        rect((6.3, 13.8, 17.7, 21), .6)
        dot(17.8, 10.8, .8)
    elif name == 'usb':
        line((12, 18.4), (12, 3))
        pen.polygon([box(p) for p in ((12, 2), (9.3, 6), (14.7, 6))], fill=color)
        line((12, 15.7), (6.1, 11.3), (6.1, 8.4))
        dot(6.1, 7.8, 1.6)
        line((12, 12.6), (17.9, 9.1), (17.9, 6.8))
        pen.rectangle(box((16.3, 4.2, 19.4, 7.2)), fill=color)
        circle((9.6, 17.4, 14.4, 22.2))
    elif name == 'network':
        rect((8.3, 2.8, 15.7, 8.6), .8)
        line((12, 8.6), (12, 12), (5.6, 12), (5.6, 15.7))
        line((12, 12), (18.4, 12), (18.4, 15.7))
        rect((2.4, 15.8, 8.8, 21.1), .8)
        rect((15.2, 15.8, 21.6, 21.1), .8)
    elif name == 'refresh':
        pen.arc(box((3.1, 3.4, 20.9, 21.2)), 202, 335, fill=color, width=width)
        pen.arc(box((3.1, 2.8, 20.9, 20.6)), 22, 155, fill=color, width=width)
        line((20, 3.7), (20, 8.8), (15.2, 8.8))
        line((4, 20.3), (4, 15.2), (8.8, 15.2))
    elif name == 'search':
        circle((3, 3, 16.9, 16.9))
        line((15, 15), (21, 21))
    elif name == 'settings':
        points = []
        for tooth in range(8):
            for angle, radius in ((-22.5, 7.3), (-12, 7.5), (-8.5, 9.5), (8.5, 9.5), (12, 7.5)):
                theta = math.radians(45 * tooth + angle)
                points.append((12 + radius * math.cos(theta), 12 + radius * math.sin(theta)))
        line(*points, points[0])
        circle((8.6, 8.6, 15.4, 15.4))
    elif name == 'log':
        rect((5, 2.6, 19, 21.4))
        for y, end in ((7, 16), (11.7, 16), (16.5, 13)):
            line((8.3, y), (end, y))
    elif name == 'folder':
        line((2.8, 7.8), (2.8, 4.5), (9.4, 4.5), (11.6, 7),
             (20.2, 7), (21, 7.8), (21, 18.7), (20, 19.7),
             (3.6, 19.7), (2.8, 18.8), (2.8, 7.8), (11.1, 7.8))
    elif name in ('eye', 'eye-off'):
        line((2, 12), (4.3, 9), (7.5, 6.9), (10.5, 6), (13.5, 6), (16.5, 6.9),
             (19.7, 9), (22, 12), (19.7, 15), (16.5, 17.1), (13.5, 18),
             (10.5, 18), (7.5, 17.1), (4.3, 15), (2, 12))
        circle((9, 9, 15, 15))
        if name == 'eye-off':
            line((3.5, 3.5), (20.5, 20.5))
    elif name == 'activity':
        line((2.5, 12.5), (7, 12.5), (10.4, 4.3), (14, 19.5), (17.2, 11.5), (21.6, 11.5))
    elif name == 'book':
        line((12, 6), (9.9, 4.7), (3, 3.6), (3, 18.1), (9.5, 19), (12, 20.4),
             (14.5, 19), (21, 18.1), (21, 3.6), (14.1, 4.7), (12, 6), (12, 20))
    elif name == 'pause':
        rect((6.3, 4.6, 9.6, 19.5), .65)
        rect((14.4, 4.6, 17.7, 19.5), .65)
    elif name == 'play':
        line((6.4, 3.9), (20, 12), (6.4, 20.1), (6.4, 3.9))
    elif name == 'swap':
        line((3.5, 7.9), (20, 7.9), (16.1, 4))
        line((20.5, 16.1), (4, 16.1), (7.9, 20))
    elif name == 'info':
        circle((2.7, 2.7, 21.3, 21.3))
        dot(12, 7.5, .95)
        line((12, 11.6), (12, 16.5))
    elif name == 'plus':
        line((12, 4.3), (12, 19.7))
        line((4.3, 12), (19.7, 12))
    elif name == 'chevron':
        line((5, 9), (12, 15.8), (19, 9))
    elif name == 'chevron-up':
        line((5, 15), (12, 8.2), (19, 15))
    else:
        raise ValueError(f'Unknown print-client icon: {name}')
    cache[key] = ImageTk.PhotoImage(image.resize((pixels, pixels), Image.Resampling.LANCZOS), master=root)
    return cache[key]


def install_theme(root):
    """Install the shared rounded styles in this window's active ttk theme."""
    root = root._root()
    style = ttk.Style(root)
    scale_key = _px(root, 100)
    elements = set(style.element_names())

    def element(part, shape, radius, outer, normal, states=()):
        name = f'Print{scale_key}{shape}.{part}'
        if name not in elements:
            pictures = [(state, _surface(root, radius, fill, border, outer)) for state, fill, border in states]
            edge = _px(root, radius) + 2
            style.element_create(name, 'image', _surface(root, radius, *normal, outer),
                                 *pictures, border=edge, width=edge*2+1, height=edge*2+1,
                                 padding=0, sticky='nsew')
            elements.add(name)
        return name

    card = element('border', 'Card', 8, BG, (WHITE, BORDER))
    service_fill = '#e3efff'
    service = element('border', 'Service', 8, BG, (service_fill, service_fill))
    style.layout('Card.TFrame', [(card, {'sticky': 'nsew'})])
    style.layout('Service.TFrame', [(service, {'sticky': 'nsew'})])
    style.configure('Service.TFrame', background=service_fill)
    style.configure('ServiceSurface.TFrame', background=service_fill, relief='flat', borderwidth=0)
    if 'PrintPages.client' not in elements:
        style.element_create('PrintPages.client', 'from', 'clam', 'padding')
    style.layout('Pages.TNotebook', [('PrintPages.client', {'sticky': 'nsew'})])
    style.configure('Pages.TNotebook', background=BG, borderwidth=0, padding=0)
    style.configure('TFrame', background=BG)
    style.configure('Card.TFrame', background=WHITE)
    style.configure('Surface.TFrame', background=WHITE, relief='flat', borderwidth=0)
    style.configure('Sidebar.TFrame', background=NAVY, relief='flat', borderwidth=0)
    style.configure('TLabel', background=BG, foreground=INK, font=(FONT, 12))
    style.configure('Card.TLabel', background=WHITE, foreground=INK)
    style.configure('CardTitle.TLabel', background=WHITE, foreground=INK, font=(FONT, 16, 'bold'))
    style.configure('Hint.TLabel', foreground=MUTED, font=(FONT, 11))
    style.configure('Title.TLabel', foreground=INK, font=(FONT, 23, 'bold'))
    style.configure('Treeview', font=(FONT, 12))
    style.configure('Treeview.Heading', font=(FONT, 11, 'bold'))
    root.option_add('*TEntry.font', (FONT, 12))
    root.option_add('*TCombobox.font', (FONT, 12))
    root.option_add('*TCombobox*Listbox.font', (FONT, 12))

    primary_states = [('disabled', '#9eb9f4', '#9eb9f4'), ('pressed', '#1042bd', '#1042bd'),
                      ('active', '#124cd9', '#124cd9'), ('focus', BLUE, '#102e8c')]
    button_styles = (
        ('TButton', 'Outline', WHITE, (WHITE, BORDER),
         [('disabled', '#f5f7fb', '#e1e6ee'), ('pressed', '#dae7ff', BLUE),
          ('active', '#f0f5ff', '#90afea'), ('focus', WHITE, BLUE)], INK),
        ('DeviceSave.TButton', 'DeviceSave', WHITE, (WHITE, BLUE),
         [('disabled', '#f5f7fb', '#e1e6ee'), ('pressed', '#dae7ff', BLUE),
          ('active', '#f0f5ff', BLUE), ('focus', WHITE, '#1042bd')], BLUE),
        ('Primary.TButton', 'Primary', WHITE, (BLUE, BLUE), primary_states, WHITE),
        ('Header.Primary.TButton', 'HeaderPrimary', BG, (BLUE, BLUE), primary_states, WHITE),
        ('Sidebar.TButton', 'Sidebar', NAVY, (NAVY, NAVY),
         [('selected', BLUE, BLUE), ('pressed', '#24457a', '#24457a'),
          ('active', '#1c3151', '#1c3151'), ('focus', NAVY, '#628bc8')], WHITE),
        ('SidebarSelected.TButton', 'SidebarSelected', NAVY, (BLUE, BLUE),
         [('active', '#235fec', '#235fec'), ('focus', BLUE, '#94b7ff')], WHITE),
    )
    for name, key, outer, normal, states, text in button_styles:
        border = element('border', key, 7, outer, normal, states)
        label = ('Button.label', {'sticky': 'nsew'})
        layer = ('Button.padding', {'sticky': 'nsew', 'children': [label]})
        style.layout(name, [(border, {'sticky': 'nsew', 'children': [layer]})])
        sidebar = name.startswith('Sidebar')
        style.configure(name, foreground=text, background=normal[0], width=0,
                        anchor='w' if sidebar else 'center', font=(FONT, 13 if sidebar else 12),
                        padding=(_px(root, 14), _px(root, 9 if sidebar else 7)))
        style.map(name, foreground=[('disabled', '#eaf0fb' if text == WHITE else '#7e8999')])

    field = element('field', 'Input', 7, WHITE, (WHITE, BORDER),
                    [('disabled', '#f2f4f8', BORDER), ('focus', WHITE, BLUE),
                     ('active', WHITE, '#a6b6cc')])
    arrow = f'Print{scale_key}.downarrow'
    if arrow not in elements:
        style.element_create(arrow, 'image', icon('chevron', 13, MUTED, root),
                             ('disabled', icon('chevron', 13, '#a2acba', root)),
                             width=_px(root, 26), sticky='')
    for name, prefix in (('TEntry', 'Entry'), ('TCombobox', 'Combobox')):
        children = []
        if prefix == 'Combobox':
            children.append((arrow, {'side': 'right', 'sticky': 'ns'}))
        children.append((f'{prefix}.padding', {'sticky': 'nsew', 'children': [
            (f'{prefix}.textarea', {'sticky': 'nsew'})]}))
        style.layout(name, [(field, {'sticky': 'nsew', 'children': children})])
        style.configure(name, padding=(_px(root, 10), _px(root, 7)), foreground=INK,
                        fieldbackground=WHITE, font=(FONT, 12))
        style.map(name, fieldbackground=[('disabled', '#f2f4f8'), ('readonly', WHITE)],
                  foreground=[('disabled', '#758197'), ('readonly', INK)])
    address = element('field', 'Address', 7, WHITE, (WHITE, BORDER),
                      [('disabled', '#f2f4f8', BORDER), ('readonly', '#f2f4f8', BORDER),
                       ('focus', WHITE, BLUE), ('active', WHITE, '#a6b6cc')])
    style.layout('ReadonlyAddress.TEntry', [(address, style.layout('TEntry')[0][1])])
    style.map('ReadonlyAddress.TEntry', fieldbackground=[('readonly', '#f2f4f8')])


def printer_badge(master, size=44):
    """Rounded app mark shared by the navigation and service summary."""
    root = master._root()
    pixels = _px(root, size)
    cache = _images(root)
    key = ('printer-badge', pixels)
    if key not in cache:
        image = Image.new('RGBA', (pixels * 4, pixels * 4))
        ImageDraw.Draw(image).rounded_rectangle(
            (0, 0, pixels * 4 - 1, pixels * 4 - 1),
            radius=_px(root, 10) * 4, fill=BLUE,
        )
        image = image.resize((pixels, pixels), Image.Resampling.LANCZOS)
        glyph = ImageTk.getimage(icon('printer', round(size * .7), WHITE, root))
        image.alpha_composite(glyph, ((pixels - glyph.width) // 2, (pixels - glyph.height) // 2))
        cache[key] = ImageTk.PhotoImage(image, master=root)
    return cache[key]
    return style
