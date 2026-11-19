# Cấu trúc thư mục `downloads/`

Mỗi thư mục con `YYYY-MM-DD/` gom các văn bản theo **Ngày ban hành**. Với mỗi văn bản có
2 file cùng tên: file tài liệu (`.docx`/`.pdf`) và file metadata `.json` đi kèm.

## Field `luoc_do` trong file `.json`

`luoc_do` là danh sách văn bản liên quan (trích từ tab "Lược đồ" trên trang gốc), gom theo
**quan hệ** với văn bản đang xét. Mỗi quan hệ là 1 key cố định (tiếng Anh, xem bảng bên
dưới) → giá trị là **danh sách docID** (số cuối URL, vd `716629`) của các văn bản liên
quan — không lưu title/url đầy đủ.

Field này **luôn có đủ toàn bộ 19 key**, kể cả khi rỗng (`[]`), để dùng ổn định — không cần
kiểm tra key có tồn tại hay không.

```json
"luoc_do": {
  "guides": [],
  "guided_by": [],
  "consolidates": ["605251", "626370"],
  "consolidated_into": [],
  "applies": [],
  "amends": [],
  "amended_by": [],
  "corrects": [],
  "corrected_by": [],
  "replaces": [],
  "replaced_by": ["84303"],
  "repeals": [],
  "repealed_by": [],
  "suspends": [],
  "suspended_by": [],
  "references": [],
  "based_on": ["665054", "662673"],
  "related_language": [],
  "related_content": ["690862", "691039"]
}
```

### Bảng key ↔ nhãn tiếng Việt trên site

Nhóm không có tiền tố ("chủ động") = văn bản NÀY thực hiện hành động đó lên văn bản khác.
Nhóm có hậu tố `_by` ("bị/được") = văn bản NÀY là đối tượng bị/được văn bản khác tác động.

| Key                 | Nhãn tiếng Việt trên site                          |
|----------------------|----------------------------------------------------|
| `guides`             | Văn bản được hướng dẫn                              |
| `guided_by`          | Văn bản hướng dẫn *(cũng gồm: Quy định chi tiết / Hướng dẫn thi hành)* |
| `consolidates`       | Văn bản được hợp nhất                               |
| `consolidated_into`  | Văn bản hợp nhất                                    |
| `applies`            | Văn bản áp dụng                                     |
| `amends`             | Văn bản sửa đổi bổ sung                             |
| `amended_by`         | Văn bản bị sửa đổi, bổ sung                         |
| `corrects`           | Văn bản đính chính                                  |
| `corrected_by`       | Văn bản bị đính chính                               |
| `replaces`           | Văn bản thay thế                                    |
| `replaced_by`        | Văn bản bị thay thế                                 |
| `repeals`            | Văn bản bãi bỏ                                      |
| `repealed_by`        | Văn bản bị bãi bỏ                                   |
| `suspends`           | Văn bản tạm ngừng hiệu lực                          |
| `suspended_by`       | Văn bản bị tạm ngừng hiệu lực                       |
| `references`         | Văn bản được dẫn chiếu                              |
| `based_on`           | Văn bản được căn cứ                                 |
| `related_language`   | Văn bản liên quan ngôn ngữ                          |
| `related_content`    | Văn bản liên quan cùng nội dung                     |

(Ví dụ dễ nhầm: **"Sửa đổi, bổ sung"** = văn bản này sửa đổi văn bản khác (`amends`), còn
**"Bị sửa đổi, bổ sung"** = văn bản này bị sửa đổi bởi văn bản khác (`amended_by`) — 2 quan
hệ ngược chiều nhau, đọc kỹ chữ "Bị" ở đầu.)

### Nhãn lạ (chưa có trong bảng trên)

Nếu site xuất hiện 1 loại quan hệ mới chưa từng thấy, dữ liệu **không bị mất** — được giữ
dưới key `unmapped:<nhãn thô tiếng Việt>`, đồng thời ghi lại vào
`../logs/unknown_luoc_do_labels.json` (đếm số lần gặp + URL ví dụ) và báo qua Telegram (1
lần/nhãn/lần chạy) để bổ sung vào bảng map trong `crawler.py` (`LUOC_DO_RELATION_MAP`).
