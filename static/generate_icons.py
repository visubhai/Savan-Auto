import os
from PIL import Image, ImageDraw

def generate_icons():
    # Make sure static directory exists
    os.makedirs("static", exist_ok=True)
    
    colors = {
        "primary": (234, 88, 12),  # #ea580c (Savan orange)
        "white": (255, 255, 255)
    }
    
    for size in (192, 512):
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        
        # Rounded background box
        padding = size // 16
        box = [padding, padding, size - padding, size - padding]
        radius = size // 5
        draw.rounded_rectangle(box, radius=radius, fill=colors["primary"])
        
        # Stylized White Bus drawing
        center_x = size // 2
        center_y = size // 2
        
        bus_width = int(size * 0.5)
        bus_height = int(size * 0.35)
        
        bx1 = center_x - bus_width // 2
        by1 = center_y - bus_height // 2 - int(size * 0.05)
        bx2 = center_x + bus_width // 2
        by2 = center_y + bus_height // 2 - int(size * 0.05)
        
        # Bus body main box
        draw.rounded_rectangle([bx1, by1, bx2, by2], radius=int(size * 0.04), fill=colors["white"])
        
        # Bus windshield / glass (upper half of the bus body)
        gw = int(bus_width * 0.86)
        gh = int(bus_height * 0.35)
        gx1 = center_x - gw // 2
        gy1 = by1 + int(bus_height * 0.1)
        gx2 = center_x + gw // 2
        gy2 = gy1 + gh
        draw.rectangle([gx1, gy1, gx2, gy2], fill=colors["primary"])
        
        # Split the windshield (two panes)
        split_w = max(1, int(size * 0.015))
        draw.rectangle([center_x - split_w // 2, gy1, center_x + split_w // 2, gy2], fill=colors["white"])
        
        # Headlights (bottom left and bottom right)
        hw = int(size * 0.04)
        hh = int(size * 0.025)
        # Left headlight
        draw.ellipse([bx1 + int(bus_width * 0.15), by2 - int(bus_height * 0.25), bx1 + int(bus_width * 0.15) + hw, by2 - int(bus_height * 0.25) + hh], fill=colors["primary"])
        # Right headlight
        draw.ellipse([bx2 - int(bus_width * 0.15) - hw, by2 - int(bus_height * 0.25), bx2 - int(bus_width * 0.15), by2 - int(bus_height * 0.25) + hh], fill=colors["primary"])
        
        # Wheels (at the bottom)
        wheel_r = int(size * 0.05)
        wy = by2
        # Left wheel
        draw.ellipse([bx1 + int(bus_width * 0.2) - wheel_r, wy - wheel_r, bx1 + int(bus_width * 0.2) + wheel_r, wy + wheel_r], fill=(30, 41, 59))
        # Right wheel
        draw.ellipse([bx2 - int(bus_width * 0.2) - wheel_r, wy - wheel_r, bx2 - int(bus_width * 0.2) + wheel_r, wy + wheel_r], fill=(30, 41, 59))
        
        # Save icon
        img.save(f"static/icon-{size}.png", "PNG")
        print(f"Generated icon-{size}.png successfully.")

if __name__ == "__main__":
    generate_icons()
