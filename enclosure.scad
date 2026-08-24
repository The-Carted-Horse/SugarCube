// Airtight enclosure for a 7.0" 800xRGBx480 LCM+CTP module
// Dimensions from the module drawing (mm).
//
// Coordinates: origin at the display center in XY.
// Z = 0 is the front face of the lens. +Z is toward the rear.
//
// Three seals keep the cavity closed:
//   1. Window gasket — front bezel against the lens glass
//   2. Flange gasket — front bezel against the rear housing
//   3. M12 cable gland — sealed cable exit through the back wall
//
// The module is located by four pins in the outer mounting holes and
// clamped by the bezel. No screw path enters the sealed volume.
//
// In Customizer, set `part` and export STL for each piece.
// Print front/rear in PETG or ASA, 5+ perimeters, 0.2 mm layers.
// Print gaskets in TPU 95A, or seat 2 mm silicone cord in the grooves.

/* [Export] */
part = "preview"; // [preview, front, rear, gasket_window, gasket_flange]

/* [Fit] */
fit_clearance = 0.40;       // extra per side around the PCB
rear_extra = 2.20;          // spare depth behind rear components

/* [Walls] */
wall = 3.00;
flange = 12.00;             // flange width beyond the tub wall
front_thick = 5.00;         // front bezel thickness at the flange
lip_thick = 2.60;           // bezel thickness over the glass
corner_r = 5.00;

/* [Screws] */
screw_d = 3.30;             // M3 clearance
screw_head_d = 6.20;
screw_head_h = 3.20;
insert_d = 4.10;            // M3 heat-set insert
insert_h = 6.20;
screw_inset = 6.00;         // outer edge to screw center

/* [Gaskets] */
gasket_w = 2.20;
gasket_h = 1.80;            // uncompressed TPU (or 2 mm cord)
groove_w = 2.40;
groove_h = 1.45;            // ~20–25% compression at full clamp
window_gasket_inset = 1.10; // window edge to inner groove edge

/* [Cable gland] */
gland_enable = true;
gland_d = 12.20;            // M12x1.5 through-hole in the back wall

/* [Hidden] */
eps = 0.08;
$fn = 48;

// --- datasheet (mm) -------------------------------------------------------
pcb_w = 164.90;
pcb_h = 102.00;
pcb_t = 1.60;

lens_w = 164.28;
lens_h = 99.17;
lens_t = 1.50;

va_w = 154.68;
va_h = 87.02;

aa_w = 154.12;
aa_h = 85.98;

lcm_ctp_t = 5.15;
to_pcb_back = 8.25;
total_t = 12.25;

outer_hole_dx = 154.89;
outer_hole_dy = 91.92;
pin_d = 2.20;
pin_h = 1.20;
pad_d = 8.00;

inner_hole_dx = 58.00;
inner_hole_dy = 49.00;
inner_from_top = 35.19;
inner_from_right = 46.54;

// --- derived --------------------------------------------------------------
inner_w = pcb_w + 2 * fit_clearance;
inner_h = pcb_h + 2 * fit_clearance;
inner_r = 1.50;

outer_w = inner_w + 2 * wall + 2 * flange;
outer_h = inner_h + 2 * wall + 2 * flange;
outer_r = corner_r;

window_w = va_w;
window_h = va_h;
window_r = 1.20;

rear_inner_z = total_t + rear_extra;
rear_outer_z = rear_inner_z + wall;
pad_h = rear_inner_z - to_pcb_back;

groove_path_w = inner_w + 2 * wall + groove_w + 1.80;
groove_path_h = inner_h + 2 * wall + groove_w + 1.80;
groove_path_r = 3.20;

win_groove_in_w = window_w + 2 * window_gasket_inset;
win_groove_in_h = window_h + 2 * window_gasket_inset;
win_groove_out_w = win_groove_in_w + 2 * groove_w;
win_groove_out_h = win_groove_in_h + 2 * groove_w;

gland_x = 0;
gland_y = -inner_h / 2 + 16;

assert(win_groove_out_w < lens_w - 0.8,
       "Window gasket does not fit on the lens land");
assert(win_groove_out_h < lens_h - 0.8,
       "Window gasket does not fit on the lens land");
assert(lip_thick > groove_h + 0.8,
       "Front lip is thinner than the window gasket groove");
assert(pad_h > 4.2,
       "Rear pads are shorter than the component stack");
assert(insert_h < rear_outer_z - 1.0,
       "Inserts break through the rear wall");

// --- 2D helpers -----------------------------------------------------------
module rounded_rect(size, r) {
    r2 = min(r, min(size.x, size.y) / 2 - 0.05);
    offset(r = r2)
        offset(delta = -r2)
            square(size, center = true);
}

module groove_2d(path_size, width, r) {
    r_out = min(r, min(path_size.x, path_size.y) / 2 - 0.05);
    r_in = max(0.40, r_out - width * 0.45);
    difference() {
        rounded_rect(path_size, r_out);
        rounded_rect(
            [path_size.x - 2 * width, path_size.y - 2 * width],
            r_in
        );
    }
}

function hole_xy(dx, dy) = [
    [ dx / 2,  dy / 2],
    [-dx / 2,  dy / 2],
    [ dx / 2, -dy / 2],
    [-dx / 2, -dy / 2]
];

function perimeter_screws() =
    let (
        x = outer_w / 2 - screw_inset,
        y = outer_h / 2 - screw_inset
    ) [
        [ x,  y], [-x,  y], [ x, -y], [-x, -y],
        [ x,  0], [-x,  0], [ 0,  y], [ 0, -y]
    ];

// --- dummy display (preview only) -----------------------------------------
module display_dummy() {
    color("#1c1c1c")
        translate([0, 0, to_pcb_back - pcb_t])
            linear_extrude(pcb_t)
                square([pcb_w, pcb_h], center = true);

    color("#2a3344")
        translate([0, 0, lens_t])
            linear_extrude(lcm_ctp_t)
                square([lens_w - 0.4, lens_h - 0.4], center = true);

    color("#c8d4e0", 0.50)
        linear_extrude(lens_t)
            square([lens_w, lens_h], center = true);

    color("#0a1220")
        translate([0, 0, -0.02])
            linear_extrude(0.04)
                square([aa_w, aa_h], center = true);
}

// --- front bezel ----------------------------------------------------------
module front_bezel() {
    difference() {
        translate([0, 0, -front_thick])
            linear_extrude(front_thick)
                rounded_rect([outer_w, outer_h], outer_r);

        // viewer-side recess: thinner picture-frame around the window
        translate([0, 0, -front_thick - eps])
            linear_extrude(front_thick - lip_thick + eps)
                rounded_rect(
                    [window_w + 8, window_h + 8],
                    window_r + 2
                );

        // viewing opening (datasheet V.A.)
        translate([0, 0, -front_thick - eps])
            linear_extrude(front_thick + 2 * eps)
                rounded_rect([window_w, window_h], window_r);

        // gasket groove in the glass-facing surface
        translate([0, 0, -groove_h])
            linear_extrude(groove_h + eps)
                groove_2d(
                    [win_groove_out_w, win_groove_out_h],
                    groove_w,
                    1.40
                );

        for (p = perimeter_screws())
            translate([p.x, p.y, 0])
                screw_through_front();
    }
}

module screw_through_front() {
    translate([0, 0, -front_thick - eps])
        cylinder(d = screw_d, h = front_thick + 2 * eps);
    translate([0, 0, -front_thick - eps])
        cylinder(d = screw_head_d, h = screw_head_h + eps);
}

// --- rear housing ---------------------------------------------------------
module rear_shell() {
    difference() {
        linear_extrude(rear_outer_z)
            rounded_rect([outer_w, outer_h], outer_r);

        translate([0, 0, -eps])
            linear_extrude(rear_inner_z + eps)
                rounded_rect([inner_w, inner_h], inner_r);
    }
}

module pcb_supports() {
    for (p = hole_xy(outer_hole_dx, outer_hole_dy)) {
        translate([p.x, p.y, to_pcb_back]) {
            cylinder(d = pad_d, h = pad_h + eps);
            translate([0, 0, -pin_h])
                cylinder(d = pin_d, h = pin_h + 0.40);
        }
    }
}

module rear_housing() {
    difference() {
        union() {
            rear_shell();
            pcb_supports();
        }

        translate([0, 0, -eps])
            linear_extrude(groove_h + eps)
                groove_2d(
                    [groove_path_w, groove_path_h],
                    groove_w,
                    groove_path_r
                );

        for (p = perimeter_screws())
            translate([p.x, p.y, -eps])
                cylinder(d = insert_d, h = insert_h + eps);

        if (gland_enable)
            gland_hole();
    }
}

module gland_hole() {
    translate([gland_x, gland_y, rear_inner_z - 1])
        cylinder(d = gland_d, h = wall + 2);
}

// --- printed gaskets (TPU) ------------------------------------------------
module gasket_window() {
    linear_extrude(gasket_h)
        groove_2d(
            [win_groove_out_w - 0.20, win_groove_out_h - 0.20],
            gasket_w,
            1.20
        );
}

module gasket_flange() {
    linear_extrude(gasket_h)
        groove_2d(
            [groove_path_w - 0.20, groove_path_h - 0.20],
            gasket_w,
            groove_path_r - 0.10
        );
}

// --- assembly preview -----------------------------------------------------
module preview() {
    explode = 18;

    color("SteelBlue")
        translate([0, 0, -explode])
            front_bezel();

    color("DarkOrange")
        translate([0, 0, -groove_h - explode * 0.45])
            gasket_window();

    display_dummy();

    color("DarkOrange")
        translate([0, 0, explode * 0.35])
            gasket_flange();

    color("Silver")
        translate([0, 0, explode])
            rear_housing();
}

echo("Outer envelope (mm)", outer_w, outer_h, front_thick + rear_outer_z);
echo("Inner cavity (mm)", inner_w, inner_h, rear_inner_z);
echo("Window VA (mm)", window_w, window_h);
echo("Gland offset from center (mm)", gland_x, gland_y);
echo("Inner PCB holes center (mm)",
     pcb_w / 2 - inner_from_right - inner_hole_dx / 2,
     pcb_h / 2 - inner_from_top - inner_hole_dy / 2);

if (part == "front")
    translate([0, 0, front_thick])
        front_bezel();
else if (part == "rear")
    translate([0, 0, rear_outer_z])
        rotate([180, 0, 0])
            rear_housing();
else if (part == "gasket_window")
    gasket_window();
else if (part == "gasket_flange")
    gasket_flange();
else
    preview();
