// SPDX-FileCopyrightText: © 2023 Tenstorrent Inc.
//
// SPDX-License-Identifier: Apache-2.0

#include "tt_eager/tt_dnn/op_library/moreh_norm/kernel_utils/common_ckernels.hpp"

namespace NAMESPACE {
void MAIN {
    int i{0};
    const auto num_rows_per_core = get_arg_val<uint32_t>(i++);
    const auto Wt = get_arg_val<uint32_t>(i++);
    const auto origin_w = get_arg_val<uint32_t>(i++);
    const auto p = get_arg_val<uint32_t>(i++);
    const bool p_is_negative = get_arg_val<uint32_t>(i++) == 1;
    const auto recip_p = get_arg_val<uint32_t>(i++);
    const bool recip_p_is_negative = get_arg_val<uint32_t>(i++) == 1;

    std::uint8_t input_id{0};
    const auto cb_x = input_id++;                // input
    const auto cb_one = input_id++;              // one
    const auto cb_decimal = input_id++;          // decimal
    const auto cb_recip_p_decimal = input_id++;  // recip_p_decimal
    const auto cb_mask_w = input_id++;           // mask_w

    std::uint8_t output_id{16};
    const auto cb_y = output_id++;  // output

    std::uint8_t intermed_id{24};
    const auto cb_tmp0 = intermed_id++;
    const auto cb_tmp1 = intermed_id++;
    const auto cb_tmp2 = intermed_id++;
    const auto cb_tmp3 = intermed_id++;
    const auto cb_tmp4 = intermed_id++;
    const auto cb_tmp5 = intermed_id++;
    const auto cb_tmp6 = intermed_id++;

    const auto cb_xabs = cb_tmp0;          // |x|
    const auto cb_xpow = cb_tmp1;          // |x|^p
    const auto cb_logx = cb_tmp2;          // log(|x|)
    const auto cb_exp_lxmd = cb_tmp3;      // exp(log(|x|) * decimal)
    const auto cb_correct_xpow = cb_tmp4;  // |x|^p * exp(log(|x|) * decimal)(==|x + decimal|^p)
    const auto cb_xpowadd = cb_tmp5;       // Add(|x + decimal|^p)
    const auto cb_xpowsum = cb_tmp6;       // Sum(|x + decimal|^p)

    constexpr uint32_t onetile = 1;
    constexpr uint32_t dst0 = 0;
    constexpr uint32_t dst1 = 1;

    binary_op_init_common(tt::CB::c_in0, tt::CB::c_in0);

    cb_wait_front(cb_one, onetile);              // comes from the reader
    cb_wait_front(cb_decimal, onetile);          // comes from the reader
    cb_wait_front(cb_recip_p_decimal, onetile);  // comes from the reader

    constexpr uint32_t TILE_W = 32;
    const bool do_mask_w = (origin_w % TILE_W) != 0;
    const auto mask_w = do_mask_w ? (origin_w % TILE_W) : TILE_W;

    if (do_mask_w) {
        cb_wait_front(cb_mask_w, onetile);  // comes from the reader
    }

    for (uint32_t row_idx = 0; row_idx < num_rows_per_core; ++row_idx) {
        for (uint32_t col_idx = 0; col_idx < Wt; ++col_idx) {
            // |x|
            ACQ();
            cb_wait_front(cb_x, onetile);  // comes from the reader
            cb_reserve_back(cb_xabs, onetile);

            copy_tile_init();
            copy_tile(cb_x, 0, dst0);

            if (do_mask_w && (col_idx == Wt - 1)) {
                copy_tile_init();
                copy_tile(cb_mask_w, 0, dst1);

                mask_tile_init();
                mask_tile(dst0, dst1);
            }

            abs_tile_init();
            abs_tile(dst0);

            pack_tile(dst0, cb_xabs);

            cb_pop_front(cb_x, onetile);
            cb_push_back(cb_xabs, onetile);
            REL();

            power_tile_to_cb(cb_xabs, cb_xpow, cb_logx, cb_decimal, cb_exp_lxmd, cb_correct_xpow, p, p_is_negative);

            // Add(|x|^p)
            if (col_idx == 0) {
                ACQ();
                cb_wait_front(cb_correct_xpow, onetile);
                cb_reserve_back(cb_xpowadd, onetile);

                copy_tile_init();
                copy_tile(cb_correct_xpow, 0, dst0);

                pack_tile(dst0, cb_xpowadd);

                cb_pop_front(cb_correct_xpow, onetile);
                cb_push_back(cb_xpowadd, onetile);
                REL();
            } else {
                ACQ();
                cb_wait_front(cb_correct_xpow, onetile);
                cb_wait_front(cb_xpowadd, onetile);
                cb_reserve_back(cb_xpowadd, onetile);

                add_tiles_init();
                add_tiles(cb_correct_xpow, cb_xpowadd, 0, 0, dst0);

                pack_tile(dst0, cb_xpowadd);

                cb_pop_front(cb_correct_xpow, onetile);
                cb_pop_front(cb_xpowadd, onetile);
                cb_push_back(cb_xpowadd, onetile);
                REL();
            }
        }
        // Sum(|x|^p)
        ACQ();
        cb_wait_front(cb_xpowadd, onetile);
        cb_reserve_back(cb_xpowsum, onetile);

        reduce_init_delta<false>(REDUCE_OP, REDUCE_DIM);
        reduce_tile(REDUCE_OP, REDUCE_DIM, cb_xpowadd, cb_one, 0, 0, dst0);
        reduce_revert_delta();

        pack_tile(dst0, cb_xpowsum);

        cb_pop_front(cb_xpowadd, onetile);
        cb_push_back(cb_xpowsum, onetile);
        REL();

        power_tile_to_cb(cb_xpowsum, cb_tmp0, cb_tmp1, cb_recip_p_decimal, cb_tmp2, cb_y, recip_p, recip_p_is_negative);
    }

    cb_pop_front(cb_one, onetile);
    cb_pop_front(cb_decimal, onetile);
    cb_pop_front(cb_recip_p_decimal, onetile);
    if (do_mask_w) {
        cb_pop_front(cb_mask_w, onetile);
    }

}  // void MAIN
}  // namespace NAMESPACE
