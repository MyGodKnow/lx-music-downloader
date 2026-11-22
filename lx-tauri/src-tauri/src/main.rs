// Prevents additional console window on Windows, DO NOT REMOVE!!
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    lx_tauri_lib::record_boot_trace();
    lx_tauri_lib::run()
}
